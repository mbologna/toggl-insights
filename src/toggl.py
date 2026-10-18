"""Toggl Reports API client — read-only time entry fetching and aggregation."""

import json
import logging
import os
import sqlite3
import statistics
import time
from datetime import UTC, date, datetime, timedelta

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.track.toggl.com/api/v9"
REPORTS_BASE_URL = "https://api.track.toggl.com/reports/api/v3"
DEFAULT_TIMEOUT = (3.05, 10)
CACHE_TTL = 3600  # 1 hour
RATE_LIMIT_MAX_RETRIES = 3
RATE_LIMIT_RETRY_DELAY_SECONDS = 30  # fallback when Toggl doesn't send a reset hint
RATE_LIMIT_RETRY_BUFFER_SECONDS = 2


def _rate_limit_wait_seconds(response: requests.Response | None) -> int:
    """Seconds to wait before retrying a 402, honoring Toggl's quota-reset hint.

    Toggl's free plan enforces an hourly quota, not a short burst limit — a 402
    response carries an X-Toggl-Quota-Resets-In header (seconds) telling us
    exactly when it clears, which can be up to ~3600s. Fall back to the fixed
    delay if the header is missing or unparseable.
    """
    reset_in = response.headers.get("x-toggl-quota-resets-in") if response is not None else None
    if reset_in is not None:
        try:
            return int(reset_in) + RATE_LIMIT_RETRY_BUFFER_SECONDS
        except ValueError:
            pass
    return RATE_LIMIT_RETRY_DELAY_SECONDS


class TogglReportsClient:
    """Read-only Toggl client for fetching and aggregating time entries."""

    def __init__(self, api_token: str, workspace_id: int) -> None:
        self.api_token = api_token
        self.workspace_id = workspace_id
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self._projects_cache: tuple[float, list[dict]] | None = None

    def _cache_key(self, project_id: int, start_date: str, end_date: str) -> str:
        return f"{project_id}:{start_date}:{end_date}"

    def _fetch_reports_page(
        self, start_date: str, end_date: str, cursor: dict | None = None
    ) -> tuple[list[dict], dict | None]:
        """One page of the Reports API's detailed search, flattened to this
        client's entry shape ({id, project_id, description, start, stop, duration}).
        Returns (entries, next_cursor_or_None).

        Unlike /me/time_entries (capped at ~90 days on start_date/since/before,
        confirmed directly against the live API), this endpoint has no such
        floor — only a 366-day span per request, enforced by the caller.
        """
        body = {"start_date": start_date, "end_date": end_date, "page_size": 1000}
        if cursor:
            body.update(cursor)

        for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):
            try:
                response = requests.post(
                    f"{REPORTS_BASE_URL}/workspace/{self.workspace_id}/search/time_entries",
                    json=body,
                    auth=(self.api_token, "api_token"),
                    timeout=DEFAULT_TIMEOUT,
                )
                response.raise_for_status()
                rows = response.json()
                entries = []
                for row in rows:
                    project_id = row.get("project_id")
                    description = row.get("description") or ""
                    for te in row.get("time_entries", []):
                        entries.append(
                            {
                                "id": te["id"],
                                "project_id": project_id,
                                "description": description,
                                "start": te["start"],
                                "stop": te.get("stop"),
                                "duration": te.get("seconds", 0),
                            }
                        )

                next_id = response.headers.get("x-next-id")
                next_cursor = None
                if next_id is not None:
                    next_cursor = {
                        "first_id": int(next_id),
                        "first_row_number": int(response.headers["x-next-row-number"]),
                        "first_timestamp": int(response.headers["x-next-timestamp"]),
                    }
                return entries, next_cursor
            except requests.exceptions.HTTPError as exc:
                is_rate_limited = exc.response is not None and exc.response.status_code == 402
                if is_rate_limited and attempt < RATE_LIMIT_MAX_RETRIES:
                    delay = _rate_limit_wait_seconds(exc.response)
                    logger.warning(
                        "Toggl rate limit reached. Retrying in %ds (%d/%d)...",
                        delay,
                        attempt + 1,
                        RATE_LIMIT_MAX_RETRIES,
                    )
                    time.sleep(delay)
                    continue
                logger.error("Failed to fetch Toggl entries: %s", exc)
                return [], None
            except requests.exceptions.RequestException as exc:
                logger.error("Failed to fetch Toggl entries: %s", exc)
                return [], None
        return [], None

    def _fetch_chunk(self, start_date: str, end_date: str) -> list[dict]:
        """Fetch every entry in a single date range chunk (must be ≤366 days)."""
        all_entries: list[dict] = []
        cursor = None
        while True:
            entries, cursor = self._fetch_reports_page(start_date, end_date, cursor)
            all_entries.extend(entries)
            if cursor is None:
                break
        return all_entries

    def _fetch_entries(self, start_date: str, end_date: str) -> list[dict]:
        """Fetch all time entries in the date range, splitting into 366-day chunks."""
        chunk_days = 366
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        all_entries: list[dict] = []
        chunk_start = start
        while chunk_start < end:
            chunk_end = min(chunk_start + timedelta(days=chunk_days), end)
            all_entries.extend(
                self._fetch_chunk(
                    chunk_start.strftime("%Y-%m-%d"),
                    chunk_end.strftime("%Y-%m-%d"),
                )
            )
            chunk_start = chunk_end + timedelta(days=1)
        return all_entries

    def get_projects(self) -> list[dict]:
        """Return all active projects, cached for 24 hours."""
        now = time.monotonic()
        if self._projects_cache is not None:
            cached_at, projects = self._projects_cache
            if now - cached_at < 86400:
                return projects
        try:
            response = requests.get(
                f"{BASE_URL}/me/projects",
                auth=(self.api_token, "api_token"),
                timeout=DEFAULT_TIMEOUT,
            )
            response.raise_for_status()
            projects = [p for p in response.json() if p.get("active")]
        except requests.exceptions.RequestException as exc:
            logger.error("Failed to fetch Toggl projects: %s", exc)
            projects = []
        self._projects_cache = (now, projects)
        return projects

    def fetch_raw(self, start_date: str, end_date: str) -> list[dict]:
        """Fetch all completed time entries in the date range (all projects, unfiltered)."""
        return [e for e in self._fetch_entries(start_date, end_date) if e.get("stop")]

    def get_project_hours(self, project_id: int, start_date: str, end_date: str) -> list[dict]:
        """Return time entries for a project in [start_date, end_date] (YYYY-MM-DD strings).

        Results are cached for 1 hour per (project_id, start_date, end_date) combination.
        """
        key = self._cache_key(project_id, start_date, end_date)
        now = time.monotonic()
        if key in self._cache:
            cached_at, cached_entries = self._cache[key]
            if now - cached_at < CACHE_TTL:
                return cached_entries

        all_entries = self._fetch_entries(start_date, end_date)
        filtered = [e for e in all_entries if e.get("project_id") == project_id and e.get("stop")]
        self._cache[key] = (now, filtered)
        return filtered

    @staticmethod
    def _entry_date(entry: dict) -> date:
        """Return the local date of an entry's start time."""
        start_str: str = entry["start"]
        if start_str.endswith("Z"):
            start_str = start_str[:-1] + "+00:00"
        return datetime.fromisoformat(start_str).date()

    @staticmethod
    def _entry_hours(entry: dict) -> float:
        """Return the duration of an entry in hours (positive)."""
        duration: int = entry.get("duration", 0)
        return max(duration, 0) / 3600.0

    @staticmethod
    def _period_key(entry_date: date, granularity: str) -> str:
        """Compute the period string for a given date and granularity."""
        if granularity == "daily":
            return entry_date.strftime("%Y-%m-%d")
        if granularity == "weekly":
            # ISO week: e.g. "2026-W26"
            iso = entry_date.isocalendar()
            return f"{iso.year}-W{iso.week:02d}"
        if granularity == "monthly":
            return entry_date.strftime("%Y-%m")
        if granularity == "yearly":
            return entry_date.strftime("%Y")
        raise ValueError(f"Unknown granularity: {granularity!r}")

    def aggregate_by_period(self, entries: list[dict], granularity: str) -> list[dict]:
        """Aggregate entries into period buckets.

        Args:
            entries: List of Toggl time entry dicts (as returned by get_project_hours).
            granularity: One of "daily", "weekly", "monthly", "yearly".

        Returns:
            List of {"period": str, "hours": float} dicts sorted by period ascending.
        """
        buckets: dict[str, float] = {}
        for entry in entries:
            entry_date = self._entry_date(entry)
            period = self._period_key(entry_date, granularity)
            buckets[period] = buckets.get(period, 0.0) + self._entry_hours(entry)

        return [{"period": p, "hours": round(h, 4)} for p, h in sorted(buckets.items())]


class TogglArchive:
    """SQLite-backed archive that accumulates time entries across API refreshes.

    A one-time backfill (see main.py's _background_sync_loop) seeds it with
    everything back to TOGGL_HISTORY_START_DATE via the Reports API; routine
    hourly syncs then just upsert the latest 90-day window on top.

    description is NULL only on rows archived before the column existed
    (added via ALTER TABLE in __init__) — entries without a description are
    stored as "". has_missing_descriptions() uses that to trigger a one-time
    re-backfill that fills them in.
    """

    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS time_entries (
            id              INTEGER PRIMARY KEY,
            project_id      INTEGER NOT NULL,
            start_iso       TEXT    NOT NULL,
            duration_secs   INTEGER NOT NULL,
            description     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_project ON time_entries (project_id);
        CREATE INDEX IF NOT EXISTS idx_day_project
            ON time_entries (substr(start_iso, 1, 10), project_id);
        CREATE TABLE IF NOT EXISTS alert_cooldowns (
            alert_type  TEXT    NOT NULL,
            project_id  INTEGER NOT NULL,
            last_alerted TEXT   NOT NULL,
            PRIMARY KEY (alert_type, project_id)
        );
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(self._SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(time_entries)")}
            if "description" not in columns:
                conn.execute("ALTER TABLE time_entries ADD COLUMN description TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_description ON time_entries (description)")

    def upsert(self, raw_entries: list[dict]) -> int:
        """Upsert completed entries (all projects) into the archive.

        Returns the number of rows written.
        """
        rows = [
            (
                e["id"],
                e["project_id"],
                e["start"],
                max(e.get("duration", 0), 0),
                e.get("description") or "",
            )
            for e in raw_entries
            if e.get("project_id") and e.get("stop") and e.get("id")
        ]
        if not rows:
            return 0
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO time_entries (id, project_id, start_iso, duration_secs, description) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def get_entries(self, project_id: int) -> list[dict]:
        """Return all archived entries for a project in aggregate-compatible format."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT start_iso, duration_secs FROM time_entries WHERE project_id = ? ORDER BY start_iso",
                (project_id,),
            ).fetchall()
        return [{"start": r["start_iso"], "duration": r["duration_secs"]} for r in rows]

    def get_description_entries(self, description: str) -> list[dict]:
        """Return all archived entries (any project) whose description matches exactly."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT start_iso, duration_secs FROM time_entries WHERE description = ? ORDER BY start_iso",
                (description,),
            ).fetchall()
        return [{"start": r["start_iso"], "duration": r["duration_secs"]} for r in rows]

    def get_described_entries(self, since: str) -> list[dict]:
        """Archived entries (any project) with a non-empty description, started on/after `since` (ISO date)."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT description, start_iso, duration_secs FROM time_entries "
                "WHERE description != '' AND start_iso >= ? AND duration_secs > 0 ORDER BY start_iso",
                (since,),
            ).fetchall()
        return [
            {"description": r["description"], "start": r["start_iso"], "duration": r["duration_secs"]} for r in rows
        ]

    def has_missing_descriptions(self) -> bool:
        """True if any row predates the description column and was never re-fetched."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT 1 FROM time_entries WHERE description IS NULL LIMIT 1").fetchone()
        return row is not None

    def clear_missing_descriptions(self) -> int:
        """Mark still-NULL descriptions as "" after a full re-backfill.

        Rows that are still NULL at that point weren't returned by Toggl at all
        (entries deleted upstream after being archived), so they'd otherwise
        re-trigger the full re-backfill on every restart.
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("UPDATE time_entries SET description = '' WHERE description IS NULL")
        return cursor.rowcount

    def is_on_cooldown(self, alert_type: str, project_id: int, cooldown_days: int) -> bool:
        """Return True if an alert of this type was sent for this project within cooldown_days."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT last_alerted FROM alert_cooldowns WHERE alert_type=? AND project_id=?",
                (alert_type, project_id),
            ).fetchone()
        if row is None:
            return False
        last = datetime.fromisoformat(row[0])
        # last_alerted is stored as UTC ISO string; compare against UTC now
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        return (datetime.now(tz=UTC) - last).days < cooldown_days

    def mark_alerted(self, alert_type: str, project_ids: list[int]) -> None:
        """Record that alerts were sent for these projects right now."""
        now = datetime.now(tz=UTC).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO alert_cooldowns (alert_type, project_id, last_alerted) VALUES (?,?,?)",
                [(alert_type, pid, now) for pid in project_ids],
            )

    def ping(self) -> None:
        """Verify the DB is reachable. Raises sqlite3.OperationalError on failure."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("SELECT 1")

    def get_daily_totals(self) -> list[dict]:
        """Return per-day totals across all projects.

        Returns a list of dicts sorted by day:
            [{"date": "2026-04-01", "total": 3.5, "by_project": {"123456789": 2.5, ...}}, ...]
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT substr(start_iso, 1, 10) AS day,
                       project_id,
                       ROUND(SUM(duration_secs) / 3600.0, 4) AS hours
                FROM time_entries
                GROUP BY day, project_id
                ORDER BY day
                """
            ).fetchall()

        # Group by day
        days: dict[str, dict] = {}
        for row in rows:
            day = row["day"]
            pid = str(row["project_id"])
            hrs = row["hours"]
            if day not in days:
                days[day] = {"date": day, "total": 0.0, "by_project": {}}
            days[day]["by_project"][pid] = hrs
            days[day]["total"] = round(days[day]["total"] + hrs, 4)

        return sorted(days.values(), key=lambda x: x["date"])

    def get_hourofday_totals(self) -> list[dict]:
        """Return per-(weekday, hour) totals from the archive using local time.

        Returns a list of dicts sorted by (dow, hour):
            [{"dow": 0, "hour": 8, "hours": 5.2}, ...]
        where dow=0 is Monday, dow=6 is Sunday.
        """
        from datetime import datetime

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT start_iso, duration_secs FROM time_entries ORDER BY start_iso").fetchall()

        buckets: dict[tuple[int, int], float] = {}
        for row in rows:
            start_str = row["start_iso"]
            try:
                if start_str.endswith("Z"):
                    start_str = start_str[:-1] + "+00:00"
                dt = datetime.fromisoformat(start_str).astimezone()
            except ValueError, OSError:
                continue
            dow = dt.weekday()  # 0=Mon..6=Sun
            hour = dt.hour
            key = (dow, hour)
            buckets[key] = buckets.get(key, 0.0) + max(row["duration_secs"], 0) / 3600.0

        return [{"dow": dow, "hour": hour, "hours": round(hrs, 4)} for (dow, hour), hrs in sorted(buckets.items())]

    def get_last_entry_time(self) -> str | None:
        """Return the start_iso of the most recently started entry, or None."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT start_iso FROM time_entries ORDER BY start_iso DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def get_earliest_entry_time(self) -> str | None:
        """Return the start_iso of the earliest archived entry, or None."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT start_iso FROM time_entries ORDER BY start_iso ASC LIMIT 1").fetchone()
        return row[0] if row else None


def date_range_for_granularity(granularity: str, now: date | None = None) -> tuple[str, str]:
    """Return (start_date, end_date) strings for the data window of each granularity."""
    today = now or date.today()
    if granularity == "daily":
        start = today - timedelta(days=90)
    elif granularity == "weekly":
        start = today - timedelta(weeks=26)
    elif granularity == "monthly":
        start = today - timedelta(days=24 * 30)
    elif granularity == "yearly":
        start = today.replace(year=today.year - 5)
    else:
        raise ValueError(f"Unknown granularity: {granularity!r}")
    return start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")


def load_ignored_descriptions() -> set[str]:
    """Parse TOGGL_IGNORED_DESCRIPTIONS env var: a JSON list of descriptions never to treat as recurring."""
    raw = os.environ.get("TOGGL_IGNORED_DESCRIPTIONS", "")
    if not raw:
        return set()
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return {str(d) for d in data}
    except json.JSONDecodeError:
        pass
    logger.warning("Invalid TOGGL_IGNORED_DESCRIPTIONS value, ignoring.")
    return set()


# (weeks, min_weeks): a description is recurring if it shows up in at least
# min_weeks distinct ISO weeks of the last `weeks`. The 8-week window is the
# current routine (what lapse nudges watch, so a habit that's really been
# dropped stops nudging after about a month); reports add the 26-week window
# so a routine that recently lapsed keeps showing its decline.
CURRENT_ROUTINE = ((8, 4),)
REPORT_ROUTINES = ((8, 4), (26, 13))


def recurring_descriptions(
    entries: list[dict], today: date, windows: tuple[tuple[int, int], ...] = CURRENT_ROUTINE
) -> list[str]:
    """Descriptions that recur in any of the given windows ending today, most frequent
    (over the last 8 weeks) first. Entries need description/start/duration; empty
    descriptions and TOGGL_IGNORED_DESCRIPTIONS are skipped."""
    ignored = load_ignored_descriptions()
    recurring: set[str] = set()
    for weeks, min_weeks in windows:
        since = today - timedelta(weeks=weeks)
        seen: dict[str, set[tuple[int, int]]] = {}
        for entry in entries:
            description = entry.get("description") or ""
            entry_date = TogglReportsClient._entry_date(entry)
            if description and description not in ignored and since < entry_date <= today:
                seen.setdefault(description, set()).add(entry_date.isocalendar()[:2])
        recurring |= {d for d, w in seen.items() if len(w) >= min_weeks}

    recent_since = today - timedelta(weeks=8)
    recent_counts = dict.fromkeys(recurring, 0)
    for entry in entries:
        if entry.get("description") in recent_counts and TogglReportsClient._entry_date(entry) > recent_since:
            recent_counts[entry["description"]] += 1
    return sorted(recurring, key=lambda d: (-recent_counts[d], d))


def session_stats(entries: list[dict]) -> dict:
    """Per-entry ("session") stats for a list of archived entries.

    Each time entry counts as one session; zero-length entries are ignored.
    Returns {"count": 0} when there's nothing to summarise, otherwise count,
    total/avg/median/min/max in seconds (ints).
    """
    durations = [e["duration"] for e in entries if e.get("duration", 0) > 0]
    if not durations:
        return {"count": 0}
    return {
        "count": len(durations),
        "total_secs": sum(durations),
        "avg_secs": round(sum(durations) / len(durations)),
        "median_secs": round(statistics.median(durations)),
        "min_secs": min(durations),
        "max_secs": max(durations),
    }


def format_duration(seconds: int) -> str:
    """Format seconds as "1h 05m" (or "45m" under an hour)."""
    hours, minutes = divmod(round(seconds / 60), 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


_SPARK_BARS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float]) -> str:
    """Unicode bar chart scaled to the largest value; only zeros get the lowest bar."""
    peak = max(values, default=0)
    if peak <= 0:
        return _SPARK_BARS[0] * len(values)
    return "".join(_SPARK_BARS[0 if v <= 0 else max(1, round(v / peak * 7))] for v in values)
