"""Toggl Insights — dashboard and trend alert server."""

import json
import logging
import os
import threading
import time
from datetime import UTC, date, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from src.toggl import (
    REPORT_ROUTINES,
    TogglArchive,
    TogglReportsClient,
    date_range_for_granularity,
    match_descriptions,
    recurring_descriptions,
    row_of,
    session_stats,
    visit_stats,
)

# uvicorn only configures its own loggers: without a root handler, this app's
# INFO lines (sync counts, removed entries) never reached the pod log.
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

load_dotenv()

app = FastAPI(title="Toggl Insights")

_DASHBOARD_HTML = os.path.join(os.path.dirname(__file__), "templates", "dashboard.html")
_DB_PATH = os.environ.get("TOGGL_DB_PATH", "/data/toggl.db")
# How far back the first full-history check reaches. The Reports API has
# no lower-bound floor (unlike /me/time_entries' ~90-day cap, confirmed
# directly against the live API), so this can safely be set deep in the past
# — an empty 366-day chunk still only costs one quota request.
_TOGGL_HISTORY_START_DATE = os.environ.get("TOGGL_HISTORY_START_DATE", "2006-01-01")
# meta keys: the earliest date a complete history check started from, and when
# the last complete one finished (UTC ISO).
_BACKFILL_FROM_KEY = "backfill_from"
_HISTORY_CHECKED_KEY = "history_checked_at"
# Local hour from which the daily history check is due: night, away from the
# daytime jobs that share Toggl's hourly quota.
_HISTORY_CHECK_HOUR = 3

_CACHE_TTL = 3600  # 1 hour
_api_cache: dict[str, tuple[float, dict]] = {}
_heatmap_cache: tuple[float, dict] | None = None
_hours_cache: tuple[float, dict] | None = None
_sessions_cache: tuple[float, dict] | None = None
_SESSIONS_WEEKLY_HISTORY_WEEKS = 26

# Set by _sync_archive_once() on every successful run; /health uses it to
# detect a background sync thread that's silently stuck in an exception loop
# (its failures are only logged — see _background_sync_loop — never surfaced
# otherwise).
_last_sync_completed_at: float | None = None
# True while the full-history check runs: on the free tier's quota it can wait
# an hour per 402, holding up the hourly sync, which /health must not take for
# a stuck thread.
_history_check_running = False
_STALE_SYNC_AFTER_SECONDS = 3 * _CACHE_TTL
# Largest share of the synced window's archived entries a single sync may
# delete as "gone from Toggl" (see _remove_deleted_entries).
_RECONCILE_MAX_FRACTION = 0.2

GRANULARITIES = ["daily", "weekly", "monthly", "yearly"]

# Module-level client so the projects cache persists across refreshes
_client: TogglReportsClient | None = None

_sync_thread_started = False


def _get_client() -> TogglReportsClient:
    global _client
    if _client is None:
        _client = TogglReportsClient(os.environ["TOGGL_API_TOKEN"], int(os.environ["TOGGL_WORKSPACE_ID"]))
    return _client


def _sync_archive_once() -> None:
    """Fetch the latest 90-day window, upsert it into the archive and drop
    archived entries that were deleted in Toggl (see _apply_window)."""
    global _last_sync_completed_at
    client = _get_client()
    archive = TogglArchive(_DB_PATH)
    start_date, end_date = date_range_for_granularity("daily")
    raw, complete = client.fetch_window(start_date, end_date)
    if complete:
        _apply_window(archive, raw, start_date, end_date)
    else:
        archive.upsert(raw)
        logger.warning("Incomplete Toggl fetch: not checking for deleted entries this run")
    logger.info("Synced %d entries (%s → %s)", len(raw), start_date, end_date)
    _last_sync_completed_at = time.monotonic()


def _apply_window(archive: TogglArchive, raw: list[dict], start_date: str, end_date: str) -> dict[str, int]:
    """Make the archive match a complete fetch of start_date..end_date: upsert it
    and drop what Toggl no longer returns. Returns how many entries were added,
    updated and removed, and how many removals were refused (see
    _remove_deleted_entries)."""
    before = archive.rows_by_id()
    stored = [e for e in raw if e.get("id") and e.get("project_id") and e.get("stop")]
    added = sum(e["id"] not in before for e in stored)
    updated = sum(e["id"] in before and before[e["id"]] != row_of(e) for e in stored)
    archive.upsert(raw)
    removed, refused = _remove_deleted_entries(archive, raw, start_date, end_date)
    return {"added": added, "updated": updated, "removed": removed, "refused": refused}


def _remove_deleted_entries(archive: TogglArchive, raw: list[dict], start_date: str, end_date: str) -> tuple[int, int]:
    """Delete archived entries in the synced window that Toggl no longer returns.
    Returns (removed, refused).

    upsert alone never forgets, so an entry deleted in Toggl would count in
    every stat forever. The window's first day is skipped: the API filters by
    the user's timezone, so entries near that boundary may fall either side.
    Refuses an empty fetch, or one that would remove more than
    _RECONCILE_MAX_FRACTION of the window: that looks like an API anomaly, not
    deletions, and the archive is the only copy of the history.
    """
    if not raw:
        return 0, 0
    first_day = (date.fromisoformat(start_date) + timedelta(days=1)).isoformat()
    missing = archive.ids_missing_from(first_day, end_date, {e["id"] for e in raw})
    if not missing:
        return 0, 0
    archived = archive.count_entries_between(first_day, end_date)
    if len(missing) > _RECONCILE_MAX_FRACTION * archived:
        logger.error(
            "Toggl no longer returns %d of %d archived entries (%s → %s); not deleting them, check the workspace",
            len(missing),
            archived,
            first_day,
            end_date,
        )
        return 0, len(missing)
    removed = archive.delete_ids(missing)
    logger.info("Removed %d archived entries deleted in Toggl: %s", removed, missing)
    return removed, 0


def _check_history_if_due(now: datetime | None = None) -> None:
    """Compare the whole archive with Toggl once a day, and repair it.

    The hourly sync covers 90 days, so without this, edits and deletions of
    older entries never reached the archive, nor did history a broken fetch
    had skipped. Due once per local day from _HISTORY_CHECK_HOUR, and at once
    when no complete check is recorded, when TOGGL_HISTORY_START_DATE moved
    earlier than a complete check reached, or when old rows still lack a
    description. That first check starts from TOGGL_HISTORY_START_DATE; later
    ones from the day before the earliest archived entry, so the empty years
    before it aren't fetched every night. An incomplete fetch only upserts what
    came back (removing on a partial list could delete valid entries) and is
    retried at the next hourly run. Every attempt is recorded for the weekly
    cleanup report.
    """
    global _history_check_running
    archive = TogglArchive(_DB_PATH)
    tz = ZoneInfo(os.environ.get("ALERT_TIMEZONE") or "Europe/Rome")
    now = now or datetime.now(tz)
    reached = archive.get_meta(_BACKFILL_FROM_KEY)
    last = archive.get_meta(_HISTORY_CHECKED_KEY)
    missing_descriptions = archive.has_missing_descriptions()
    from_start = reached is None or reached > _TOGGL_HISTORY_START_DATE
    new_day = last is None or (
        datetime.fromisoformat(last).astimezone(tz).date() < now.date() and now.hour >= _HISTORY_CHECK_HOUR
    )
    if not (from_start or missing_descriptions or new_day):
        return

    earliest = archive.get_earliest_entry_time()
    if from_start or earliest is None:
        start_date = _TOGGL_HISTORY_START_DATE
    else:
        start_date = (date.fromisoformat(earliest[:10]) - timedelta(days=1)).isoformat()
    if earliest is not None:
        start_date = min(start_date, earliest[:10])
    end_date = now.date().isoformat()

    logger.info("Checking the archive against Toggl (%s → %s)", start_date, end_date)
    _history_check_running = True
    try:
        raw, complete = _get_client().fetch_window(start_date, end_date)
    finally:
        _history_check_running = False
    if not complete:
        archive.upsert(raw)
        archive.record_history_check(complete=False)
        logger.warning("History check incomplete: nothing removed, retrying at the next run")
        return

    counts = _apply_window(archive, raw, start_date, end_date)
    archive.record_history_check(complete=True, **counts)
    if from_start:
        archive.set_meta(_BACKFILL_FROM_KEY, start_date)
    archive.set_meta(_HISTORY_CHECKED_KEY, datetime.now(tz=UTC).isoformat())
    logger.info(
        "History check: %d added, %d updated, %d removed, %d removals refused",
        counts["added"],
        counts["updated"],
        counts["removed"],
        counts["refused"],
    )
    # Only trust a non-empty fetch: clearing after an empty one would
    # permanently skip the description re-backfill.
    if missing_descriptions and raw:
        cleared = archive.clear_missing_descriptions()
        if cleared:
            logger.info("Marked %d archived entries no longer in Toggl as description-less", cleared)


def _background_sync_loop() -> None:
    """Continuously keep the archive fresh, off the request path.

    Runs in its own thread so a 402 can block on Toggl's real hourly quota
    reset (via TogglReportsClient's retry/backoff) without ever stalling a
    live dashboard request. Each hour: the 90-day sync, then the daily
    full-history check when it is due (after the sync, so its counts are
    corrections rather than entries the sync would have brought anyway).
    """
    while True:
        try:
            _sync_archive_once()
        except Exception:
            logger.exception("Background Toggl sync failed")
        try:
            _check_history_if_due()
        except Exception:
            logger.exception("History check against Toggl failed")
        time.sleep(_CACHE_TTL)


@app.on_event("startup")
def _start_background_sync() -> None:
    global _sync_thread_started
    if _sync_thread_started:
        return
    _sync_thread_started = True
    threading.Thread(target=_background_sync_loop, daemon=True, name="toggl-sync").start()


def _load_targets() -> dict:
    """Parse TOGGL_WEEKLY_TARGETS env var as JSON. Returns empty dict on missing/invalid."""
    raw = os.environ.get("TOGGL_WEEKLY_TARGETS", "")
    if not raw:
        return {}
    try:
        targets = json.loads(raw)
        if isinstance(targets, dict):
            return {str(k): float(v) for k, v in targets.items()}
    except json.JSONDecodeError, ValueError, TypeError:
        logger.warning("Invalid TOGGL_WEEKLY_TARGETS value, ignoring.")
    return {}


def _load_yearly_goals() -> dict:
    """Parse TOGGL_YEARLY_GOALS env var as JSON. Returns empty dict on missing/invalid."""
    raw = os.environ.get("TOGGL_YEARLY_GOALS", "")
    if not raw:
        return {}
    try:
        goals = json.loads(raw)
        if isinstance(goals, dict):
            return {str(k): float(v) for k, v in goals.items()}
    except json.JSONDecodeError, ValueError, TypeError:
        logger.warning("Invalid TOGGL_YEARLY_GOALS value, ignoring.")
    return {}


def _build_api_data() -> dict:
    """Aggregate all projects from the archive.

    The archive is kept fresh by a background thread (see
    _background_sync_loop), so this reads from SQLite only — no Toggl API
    calls, and so no risk of a request blocking on Toggl's rate limit.

    Response shape:
        {
            "projects": [{"id": 123, "name": "..."}, ...],
            "daily":    [{"period": "2026-04-01", "123": 1.5, "456": 0.0, ...}, ...],
            "weekly":   [...],
            "monthly":  [...],
            "yearly":   [...],
            "targets":  {"123": 5.0, ...},
        }
    """
    client = _get_client()
    archive = TogglArchive(_DB_PATH)

    # Discover all active projects (cached 24 h in client)
    projects = client.get_projects()
    project_ids = [p["id"] for p in projects]

    result: dict = {
        "projects": [{"id": p["id"], "name": p["name"], "color": p.get("color", "")} for p in projects],
        "targets": _load_targets(),
        "yearly_goals": _load_yearly_goals(),
    }

    for granularity in GRANULARITIES:
        periods: dict[str, dict] = {}
        for pid in project_ids:
            entries = archive.get_entries(pid)
            for item in client.aggregate_by_period(entries, granularity):
                key = item["period"]
                if key not in periods:
                    periods[key] = {"period": key}
                periods[key][str(pid)] = item["hours"]

        # Fill zeros for projects absent in a period
        pid_strs = [str(pid) for pid in project_ids]
        for row in periods.values():
            for pid_str in pid_strs:
                row.setdefault(pid_str, 0.0)

        result[granularity] = sorted(periods.values(), key=lambda x: x["period"])

    return result


def _cached_api_data() -> dict:
    cache_key = "api_data"
    now = time.monotonic()
    if cache_key in _api_cache:
        cached_at, data = _api_cache[cache_key]
        if now - cached_at < _CACHE_TTL:
            return data
    data = _build_api_data()
    _api_cache[cache_key] = (now, data)
    return data


@app.get("/health")
def health() -> dict:
    try:
        TogglArchive(_DB_PATH).ping()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if _last_sync_completed_at is not None and not _history_check_running:
        stale_for = time.monotonic() - _last_sync_completed_at
        if stale_for > _STALE_SYNC_AFTER_SECONDS:
            raise HTTPException(
                status_code=503,
                detail=f"background sync stale for {stale_for:.0f}s (last success over {_STALE_SYNC_AFTER_SECONDS}s ago)",
            )

    return {"status": "ok"}


@app.get("/api/data")
def api_data() -> dict:
    return _cached_api_data()


@app.get("/api/heatmap")
def api_heatmap() -> dict:
    global _heatmap_cache
    now = time.monotonic()
    if _heatmap_cache is not None:
        cached_at, data = _heatmap_cache
        if now - cached_at < _CACHE_TTL:
            return data
    archive = TogglArchive(_DB_PATH)
    daily_target_raw = os.environ.get("TOGGL_DAILY_TARGET_HOURS", "")
    data = {
        "days": archive.get_daily_totals(),
        "last_entry_iso": archive.get_last_entry_time(),
        "daily_target": float(daily_target_raw) if daily_target_raw else None,
    }
    _heatmap_cache = (now, data)
    return data


@app.get("/api/hours")
def api_hours() -> dict:
    global _hours_cache
    now = time.monotonic()
    if _hours_cache is not None:
        cached_at, data = _hours_cache
        if now - cached_at < _CACHE_TTL:
            return data
    archive = TogglArchive(_DB_PATH)
    data = {"slots": archive.get_hourofday_totals()}
    _hours_cache = (now, data)
    return data


def _build_sessions_data(archive: TogglArchive, today: date) -> dict:
    """Session stats for each recurring entry description (see recurring_descriptions).

    Response shape:
        {
            "descriptions": [
                {
                    "description": "...",
                    "last_4_weeks":  {"count": 3, "avg_secs": ..., ...},
                    "last_12_weeks": {...},
                    "all_time":      {...},
                    "weekly": [{"period": "2026-W26", "count": 3, "avg_secs": 4500}, ...],
                },
                ...
            ]
        }
    """
    weekly_since = today - timedelta(weeks=_SESSIONS_WEEKLY_HISTORY_WEEKS)
    recent = archive.get_described_entries(since=weekly_since.isoformat())
    result = []
    for description in recurring_descriptions(recent, today, REPORT_ROUTINES):
        entries = [e for e in archive.get_description_entries(description) if e["duration"] > 0]
        dated = [(TogglReportsClient._entry_date(e), e) for e in entries]

        weeks: dict[str, list[dict]] = {}
        for entry_date, entry in dated:
            if entry_date >= weekly_since:
                weeks.setdefault(TogglReportsClient._period_key(entry_date, "weekly"), []).append(entry)

        result.append(
            {
                "description": description,
                "last_4_weeks": session_stats([e for d, e in dated if d > today - timedelta(weeks=4)]),
                "last_12_weeks": session_stats([e for d, e in dated if d > today - timedelta(weeks=12)]),
                "all_time": session_stats(entries),
                "weekly": [
                    {"period": period, "count": stats["count"], "avg_secs": stats["avg_secs"]}
                    for period, stats in ((p, session_stats(es)) for p, es in sorted(weeks.items()))
                ],
            }
        )
    return {"descriptions": result}


@app.get("/api/sessions")
def api_sessions() -> dict:
    global _sessions_cache
    now = time.monotonic()
    if _sessions_cache is not None:
        cached_at, data = _sessions_cache
        if now - cached_at < _CACHE_TTL:
            return data
    data = _build_sessions_data(TogglArchive(_DB_PATH), date.today())
    _sessions_cache = (now, data)
    return data


def _build_typical_session(archive: TogglArchive, query: str, weeks: int, today: date) -> dict:
    """Typical length of one description's visits over the last `weeks` full weeks.

    `query` is matched by match_descriptions (case, emoji and punctuation
    ignored), so callers that can't easily send emoji (an iOS Shortcut) can pass
    the title's words. Today is left out: it is usually still in progress. Raises
    404 when nothing matches and 409 when the query matches more than one
    description.
    """
    since = today - timedelta(weeks=weeks)
    entries = [
        e
        for e in archive.get_described_entries(since=since.isoformat())
        if since <= TogglReportsClient._entry_date(e) < today
    ]
    matches = match_descriptions(query, (e["description"] for e in entries))
    if not matches:
        raise HTTPException(status_code=404, detail=f"no entries matching {query!r} in the last {weeks} weeks")
    if len(matches) > 1:
        raise HTTPException(
            status_code=409, detail={"error": f"{query!r} matches several descriptions", "matches": matches}
        )

    (description,) = matches
    stats = visit_stats([e for e in entries if e["description"] == description])
    return {
        "description": description,
        "weeks": weeks,
        "since": since.isoformat(),
        "until": (today - timedelta(days=1)).isoformat(),
        "visits": stats["count"],
        "minutes": round(stats["median_secs"] / 60),
        "median_secs": stats["median_secs"],
        "avg_secs": stats["avg_secs"],
        "min_secs": stats["min_secs"],
        "max_secs": stats["max_secs"],
    }


@app.get("/api/sessions/typical")
def api_sessions_typical(
    description: Annotated[str, Query(min_length=1)],
    weeks: Annotated[int, Query(ge=1, le=52)] = 4,
) -> dict:
    return _build_typical_session(TogglArchive(_DB_PATH), description, weeks, date.today())


@app.get("/", response_class=FileResponse)
def dashboard() -> FileResponse:
    return FileResponse(_DASHBOARD_HTML, media_type="text/html")
