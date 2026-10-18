"""Scheduled Apprise notifications: periodic aggregate reports plus one nudge.

Usage:
    python -m src.alerts --digest       # weekly digest of the last completed ISO week (run Monday morning)
    python -m src.alerts --monthly      # monthly report of the last completed month (run on the 1st)
    python -m src.alerts --session-gap  # nudge when a recurring activity has lapsed (run daily)
"""

import calendar
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Allow running as `python src/alerts.py` from project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.toggl import (  # noqa: E402
    CURRENT_ROUTINE,
    REPORT_ROUTINES,
    TogglArchive,
    TogglReportsClient,
    format_duration,
    recurring_descriptions,
    session_stats,
    sparkline,
)

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

_ALERT_TZ = ZoneInfo(os.environ.get("ALERT_TIMEZONE", "UTC"))


def _entry_local_date(start_str: str) -> date:
    """Return the local date of a time entry start timestamp (respects ALERT_TIMEZONE)."""
    if start_str.endswith("Z"):
        start_str = start_str[:-1] + "+00:00"
    return datetime.fromisoformat(start_str).astimezone(_ALERT_TZ).date()


def _iso_week_start(iso_year: int, iso_week: int) -> date:
    """Return the Monday of the given ISO week."""
    jan4 = date(iso_year, 1, 4)
    week1_monday = jan4 - timedelta(days=jan4.weekday())
    return week1_monday + timedelta(weeks=iso_week - 1)


def _hours_for_week(entries: list[dict], iso_year: int, iso_week: int) -> float:
    """Sum hours for entries that fall within the given ISO week."""
    week_start = _iso_week_start(iso_year, iso_week)
    week_end = week_start + timedelta(days=7)
    total = 0.0
    for entry in entries:
        entry_date = _entry_local_date(entry["start"])
        duration = max(entry.get("duration", 0), 0)
        if week_start <= entry_date < week_end:
            total += duration / 3600.0
    return round(total, 4)


def _last_completed_iso_week(today: date) -> tuple[int, int]:
    """Return (iso_year, iso_week) of the most recently completed ISO week."""
    # 'last week' = the ISO week that ended last Sunday
    last_monday = today - timedelta(days=today.weekday() + 7)
    iso = last_monday.isocalendar()
    return iso.year, iso.week


def _rolling_weekly_avg(entries: list[dict], today: date, weeks: int = 4) -> float:
    """Return the average weekly hours over the last `weeks` completed weeks."""
    last_year, last_week = _last_completed_iso_week(today)
    week_hours: list[float] = []
    ref_year, ref_week = last_year, last_week
    for _ in range(weeks):
        week_hours.append(_hours_for_week(entries, ref_year, ref_week))
        prev_monday = _iso_week_start(ref_year, ref_week) - timedelta(weeks=1)
        iso = prev_monday.isocalendar()
        ref_year, ref_week = iso.year, iso.week
    return sum(week_hours) / len(week_hours) if week_hours else 0.0


def _hours_for_month(entries: list[dict], year: int, month: int) -> float:
    """Sum hours for entries that fall within the given calendar month."""
    month_start = date(year, month, 1)
    if month == 12:
        month_end = date(year + 1, 1, 1)
    else:
        month_end = date(year, month + 1, 1)
    total = 0.0
    for entry in entries:
        entry_date = _entry_local_date(entry["start"])
        duration = max(entry.get("duration", 0), 0)
        if month_start <= entry_date < month_end:
            total += duration / 3600.0
    return round(total, 4)


def _rolling_monthly_avg(entries: list[dict], today: date, months: int = 3) -> float:
    """Return the average monthly hours over the last `months` completed months."""
    month_hours: list[float] = []
    year, month = today.year, today.month
    for _ in range(months):
        month -= 1
        if month == 0:
            month = 12
            year -= 1
        month_hours.append(_hours_for_month(entries, year, month))
    return sum(month_hours) / len(month_hours) if month_hours else 0.0


def _load_per_project_thresholds() -> dict[str, float]:
    """Parse TREND_ALERT_THRESHOLDS env var as JSON map of project_id → threshold."""
    raw = os.environ.get("TREND_ALERT_THRESHOLDS", "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {str(k): float(v) for k, v in data.items()}
    except json.JSONDecodeError, ValueError, TypeError:
        logger.warning("Invalid TREND_ALERT_THRESHOLDS value, ignoring.")
    return {}


# Apprise sits behind a cross-cluster Tailscale path that goes cold between
# infrequent alert runs; the first (cold) WireGuard connect can take several
# seconds. A tight connect timeout with no retry silently dropped alerts, so
# allow a generous connect timeout and retry — the retry hits a now-warm path.
_APPRISE_CONNECT_TIMEOUT = float(os.environ.get("APPRISE_CONNECT_TIMEOUT", "15"))
_APPRISE_READ_TIMEOUT = float(os.environ.get("APPRISE_READ_TIMEOUT", "15"))
_APPRISE_RETRIES = int(os.environ.get("APPRISE_RETRIES", "4"))


def _build_apprise_session() -> requests.Session:
    """Session that retries the Apprise POST on connection errors and 5xx.

    urllib3's Retry does not retry POST by default (not idempotent); we opt in
    explicitly because the Apprise notify call is safe to repeat. backoff_factor
    1.5 → waits ~0s, 1.5s, 3s, 6s between attempts, giving the cold tailnet path
    time to come up.
    """
    retry = Retry(
        total=_APPRISE_RETRIES,
        connect=_APPRISE_RETRIES,
        read=2,
        status=2,
        backoff_factor=1.5,
        status_forcelist=(502, 503, 504),
        allowed_methods=frozenset({"POST"}),
        raise_on_status=False,
    )
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _get_client_and_archive() -> tuple[TogglReportsClient, TogglArchive]:
    """Build a Toggl client + archive, reading from the archive as-is.

    Deliberately does NOT sync fresh entries from the Reports API — every
    entrypoint below used to independently re-fetch the latest 90-120 days on
    every run, each drawing from the same hourly Reports API quota as all the
    other alert CronJobs *and* the dashboard's own background sync thread
    (src/main.py's _background_sync_loop), which already keeps this same
    SQLite archive (shared via the toggl-insights-data PVC) fresh at least
    hourly. Reading it as-is here means alerts are never more than ~1h stale
    and no longer burn their own share of the quota at all.
    """
    api_token = os.environ["TOGGL_API_TOKEN"]
    workspace_id = int(os.environ["TOGGL_WORKSPACE_ID"])
    db_path = os.environ.get("TOGGL_DB_PATH", "/data/toggl.db")

    client = TogglReportsClient(api_token, workspace_id)
    archive = TogglArchive(db_path)
    return client, archive


def _is_dry_run() -> bool:
    return os.environ.get("ALERT_DRY_RUN", "").lower() in ("1", "true", "yes")


def _send_alert(apprise_url: str, title: str, lines: list[str]) -> bool:
    """POST alert to Apprise. Returns True on success. Logs only if ALERT_DRY_RUN is set."""
    if _is_dry_run():
        logger.info("DRY RUN — would send '%s':\n%s", title, "\n".join(lines))
        return True  # treat as success so cooldowns are still updated
    body = "\n".join(lines)
    try:
        with _build_apprise_session() as session:
            response = session.post(
                apprise_url,
                json={"title": title, "body": body},
                timeout=(_APPRISE_CONNECT_TIMEOUT, _APPRISE_READ_TIMEOUT),
            )
        response.raise_for_status()
        logger.info("Alert sent successfully.")
        return True
    except requests.exceptions.RequestException as exc:
        logger.error("Failed to send Apprise alert: %s", exc)
        return False


def _alert_on_empty_projects(archive: TogglArchive, apprise_url: str) -> None:
    """Meta-alert for the one failure mode every run_* function used to just
    log and silently swallow: get_projects() returning nothing, whether from
    an expired/invalid token, a wrong workspace ID, a Toggl outage, or (far
    less likely) a genuinely empty workspace. Without this, a broken token
    means no trend/pace/digest/over-target alert ever fires again, with
    nothing surfacing that anywhere — the same kind of silence that let the
    workspace_id bug go unnoticed for as long as it did.

    project_id 0 is a sentinel reserved for alerts not tied to one project.
    """
    cooldown_days = int(os.environ.get("ALERT_COOLDOWN_DAYS", "5"))
    if archive.is_on_cooldown("no_projects", 0, cooldown_days):
        logger.info("Skipping no-projects alert — on cooldown (%dd)", cooldown_days)
        return

    lines = [
        "Toggl returned no active projects — the API token may be invalid or "
        "expired, the workspace ID may be wrong, or Toggl itself may be down. "
        "No digest or monthly report can run until this is fixed.",
    ]
    if _send_alert(apprise_url, "Toggl Insights: alerting is down", lines):
        archive.mark_alerted("no_projects", [0])


def _projects_or_alert(client: TogglReportsClient, archive: TogglArchive, apprise_url: str) -> list[dict]:
    """Active projects, raising the no-projects meta-alert when there are none."""
    projects = client.get_projects()
    if not projects:
        logger.warning("No active projects found.")
        _alert_on_empty_projects(archive, apprise_url)
    return projects


# Changes smaller than this are left out of messages entirely — "+2%" is noise.
_NOISE_THRESHOLD = 0.05


def _change(current: float, baseline: float, threshold: float) -> str:
    """Change vs baseline as e.g. "-38% ↓": empty when negligible, arrow only beyond threshold."""
    if baseline == 0:
        return "new" if current > 0 else ""
    change = (current - baseline) / baseline
    if abs(change) < _NOISE_THRESHOLD:
        return ""
    text = f"{change * 100:+.0f}%"
    if abs(change) > threshold:
        text += " ↑" if change > 0 else " ↓"
    return text


def _join(*parts: str) -> str:
    """Join the non-empty message fragments with a double space."""
    return "  ".join(p for p in parts if p)


def _rate(value: float) -> str:
    """1 decimal, without a trailing .0 (2.8, 18)."""
    return f"{value:.1f}".removesuffix(".0")


def _day_month(d: date) -> str:
    return f"{d.day} {d:%b}"


def _week_label(week_start: date) -> str:
    """Week range label: 5–11 Oct, 29 Sep – 5 Oct, or 29 Dec 2025 – 4 Jan 2026."""
    week_end = week_start + timedelta(days=6)
    if week_start.year != week_end.year:
        return f"{_day_month(week_start)} {week_start.year} – {_day_month(week_end)} {week_end.year}"
    if week_start.month != week_end.month:
        return f"{_day_month(week_start)} – {_day_month(week_end)}"
    return f"{week_start.day}–{_day_month(week_end)}"


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _sessions_in(entries: list[dict], start: date, end: date) -> list[dict]:
    """Entries whose local start date falls in [start, end)."""
    return [e for e in entries if start <= _entry_local_date(e["start"]) < end]


def _per_week(count: int, start: date, end: date) -> float:
    return count / ((end - start).days / 7)


def _recurring_entries(
    archive: TogglArchive, today: date, windows: tuple[tuple[int, int], ...], since: date
) -> dict[str, list[dict]]:
    """Recurring descriptions (most frequent first) → their entries since `since`."""
    entries = archive.get_described_entries(since=since.isoformat())
    by_description: dict[str, list[dict]] = {d: [] for d in recurring_descriptions(entries, today, windows)}
    for entry in entries:
        if entry["description"] in by_description:
            by_description[entry["description"]].append(entry)
    return by_description


# ---------------------------------------------------------------------------
# Weekly digest
# ---------------------------------------------------------------------------


def _load_weekly_targets() -> dict[str, float]:
    """Parse TOGGL_WEEKLY_TARGETS env var as JSON map of project_id → target hours."""
    raw = os.environ.get("TOGGL_WEEKLY_TARGETS", "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {str(k): float(v) for k, v in data.items()}
    except json.JSONDecodeError, ValueError, TypeError:
        logger.warning("Invalid TOGGL_WEEKLY_TARGETS value, ignoring.")
    return {}


def _target_suffix(hours: float, target: float, over_threshold: float) -> str:
    """Target marker for a digest line: ✓ when met, ⚠ when exceeded by more than over_threshold."""
    suffix = f" · target {target:.1f} h"
    if hours > target * (1.0 + over_threshold):
        suffix += f" ⚠ +{(hours - target) / target * 100:.0f}%"
    elif hours >= target:
        suffix += " ✓"
    return suffix


def _digest_recurring_lines(by_description: dict[str, list[dict]], week_start: date) -> list[str]:
    """One line per recurring description: the week's count and average length,
    against its usual rate/length over the 4 weeks before."""
    week_end = week_start + timedelta(days=7)
    prior_start = week_start - timedelta(weeks=4)
    lines = []
    for description, entries in by_description.items():
        week = session_stats(_sessions_in(entries, week_start, week_end))
        prior = session_stats(_sessions_in(entries, prior_start, week_start))
        usual_rate = f"usually {_rate(prior['count'] / 4)}×/wk" if prior["count"] else ""
        if week["count"] == 0:
            line = f"• {description}: none" + (f" ({usual_rate})" if usual_rate else "")
        else:
            line = f"• {description}: {week['count']}× · avg {format_duration(week['avg_secs'])}"
            if usual_rate:
                line += f" ({usual_rate} · {format_duration(prior['avg_secs'])})"
        logger.info("Recurring — %s", line)
        lines.append(line)
    return lines


def run_weekly_digest() -> None:
    """Summarise the last completed ISO week: hours per project against the 4 weeks
    before it and any weekly target, plus every recurring activity.

    Run it on Monday morning — "last completed week" is the one that ended on
    the most recent Sunday *before* today, so a Sunday run would report a week
    that's already 8 days stale.
    """
    apprise_url = os.environ["APPRISE_URL"]
    threshold = float(os.environ.get("TREND_ALERT_THRESHOLD", "0.25"))
    over_threshold = float(os.environ.get("OVER_TARGET_THRESHOLD", "0.5"))

    client, archive = _get_client_and_archive()
    today = date.today()

    projects = _projects_or_alert(client, archive, apprise_url)
    if not projects:
        return

    last_year, last_week = _last_completed_iso_week(today)
    week_start = _iso_week_start(last_year, last_week)
    per_thresholds = _load_per_project_thresholds()
    targets = _load_weekly_targets()

    # (hours, 4-week avg before it, line)
    rows: list[tuple[float, float, str]] = []
    for project in projects:
        project_id = project["id"]
        entries = archive.get_entries(project_id)
        hours = _hours_for_week(entries, last_year, last_week)
        # Shifting "today" back a week makes the rolling window end the week
        # *before* last_week, so last_week is compared against the 4 before it.
        avg = _rolling_weekly_avg(entries, today - timedelta(weeks=1), weeks=4)
        target = targets.get(str(project_id))
        if hours == 0 and avg == 0 and target is None:
            continue
        change = _change(hours, avg, per_thresholds.get(str(project_id), threshold))
        line = _join(f"• {project['name']}: {hours:.1f} h", change)
        if target is not None:
            line += _target_suffix(hours, target, over_threshold)
        rows.append((hours, avg, line))
        logger.info("%s: %.1fh in W%d %d (avg %.1fh)", project["name"], hours, last_week, last_year, avg)

    total = sum(r[0] for r in rows)
    if total == 0.0:
        logger.info("No entries for W%d %d — skipping digest.", last_week, last_year)
        return

    rows.sort(key=lambda r: r[0], reverse=True)
    lines = [r[2] for r in rows]
    lines.append(_join(f"Total: {total:.1f} h", _change(total, sum(r[1] for r in rows), threshold)))

    # Detected as of the reported week, so the week's own entries don't decide it.
    week_end = week_start + timedelta(days=7)
    recurring = _recurring_entries(
        archive, week_end - timedelta(days=1), REPORT_ROUTINES, since=week_end - timedelta(weeks=26)
    )
    if recurring:
        lines += ["", "Recurring:", *_digest_recurring_lines(recurring, week_start)]
    lines += ["", "% vs average of the previous 4 weeks"]

    _send_alert(apprise_url, f"Week {_week_label(week_start)}", lines)


# ---------------------------------------------------------------------------
# Monthly report
# ---------------------------------------------------------------------------

_SPARK_MONTHS = 6


def _monthly_recurring_lines(
    by_description: dict[str, list[dict]], months: list[tuple[date, date]], threshold: float
) -> list[str]:
    """One line per recurring description: the month's count and weekly rate, a
    6-month sparkline of that rate, and average length vs the 3 months before."""
    month_start, month_end = months[-1]
    prior_start = months[-4][0]
    lines = []
    for description, entries in by_description.items():
        rates = [_per_week(len(_sessions_in(entries, s, e)), s, e) for s, e in months]
        month = session_stats(_sessions_in(entries, month_start, month_end))
        prior = session_stats(_sessions_in(entries, prior_start, month_start))
        if month["count"] == 0:
            line = _join(f"• {description}: none", sparkline(rates))
        else:
            avg_change = _change(month["avg_secs"], prior.get("avg_secs", 0), threshold) if prior["count"] else ""
            line = _join(
                f"• {description}: {month['count']}× ({_rate(rates[-1])}/wk)",
                f"{sparkline(rates)} · avg {format_duration(month['avg_secs'])}",
                avg_change,
            )
        logger.info("Recurring — %s", line)
        lines.append(line)
    return lines


def run_monthly_report() -> None:
    """Summarise the last completed month: hours per project against the 3 months
    before it with a 6-month sparkline, plus every recurring activity's rate and
    length. Run on the 1st."""
    apprise_url = os.environ["APPRISE_URL"]
    threshold = float(os.environ.get("TREND_ALERT_THRESHOLD", "0.25"))

    client, archive = _get_client_and_archive()
    today = date.today()

    projects = _projects_or_alert(client, archive, apprise_url)
    if not projects:
        return

    year, month = _shift_month(today.year, today.month, -1)
    month_start = date(year, month, 1)
    # [(start, end)] for the last 6 months, oldest first, ending with the reported one
    months = []
    for back in range(_SPARK_MONTHS - 1, -1, -1):
        y, m = _shift_month(year, month, -back)
        ny, nm = _shift_month(y, m, 1)
        months.append((date(y, m, 1), date(ny, nm, 1)))
    per_thresholds = _load_per_project_thresholds()

    # (hours, 3-mo avg before it, line, per-month hours)
    rows: list[tuple[float, float, str, list[float]]] = []
    for project in projects:
        project_id = project["id"]
        entries = archive.get_entries(project_id)
        monthly = [_hours_for_month(entries, s.year, s.month) for s, _ in months]
        hours = monthly[-1]
        avg = _rolling_monthly_avg(entries, month_start, months=3)
        if hours == 0 and avg == 0:
            continue
        change = _change(hours, avg, per_thresholds.get(str(project_id), threshold))
        rows.append((hours, avg, _join(f"• {project['name']}: {hours:.1f} h", change, sparkline(monthly)), monthly))
        logger.info("%s: %.1fh in %d-%02d (avg %.1fh)", project["name"], hours, year, month, avg)

    total = sum(r[0] for r in rows)
    if total == 0.0:
        logger.info("No entries for %d-%02d — skipping monthly report.", year, month)
        return

    rows.sort(key=lambda r: r[0], reverse=True)
    lines = [r[2] for r in rows]
    totals = [sum(r[3][i] for r in rows) for i in range(_SPARK_MONTHS)]
    lines.append(_join(f"Total: {total:.1f} h", _change(total, sum(r[1] for r in rows), threshold), sparkline(totals)))

    month_end = months[-1][1]
    recurring = _recurring_entries(archive, month_end - timedelta(days=1), REPORT_ROUTINES, since=months[0][0])
    if recurring:
        lines += ["", "Recurring:", *_monthly_recurring_lines(recurring, months, threshold)]
    lines += ["", "% vs average of the previous 3 months · bars = last 6 months"]

    _send_alert(apprise_url, f"{calendar.month_name[month]} {year}", lines)


# ---------------------------------------------------------------------------
# Lapsed routines
# ---------------------------------------------------------------------------

# Never call a gap shorter than this a lapse, however frequent the routine.
_MIN_GAP_DAYS = 3


def run_session_gap_alerts() -> None:
    """Nudge when a recurring activity (in its current 8-week routine) has gone
    about twice its usual interval between active days without happening.

    The interval is measured in active *days* (several entries on one day count
    once, e.g. errands). Cooldowns are per description (alert_type
    "session_gap:<description>", project_id 0 = not tied to one project), so
    while a gap lasts it repeats at most every ALERT_COOLDOWN_DAYS — and it
    stops once the routine drops out of the 8-week window.
    """
    apprise_url = os.environ["APPRISE_URL"]
    cooldown_days = int(os.environ.get("ALERT_COOLDOWN_DAYS", "5"))

    _, archive = _get_client_and_archive()
    today = date.today()
    window_weeks = CURRENT_ROUTINE[0][0]
    window_start = today - timedelta(weeks=window_weeks)
    recurring = _recurring_entries(archive, today, CURRENT_ROUTINE, since=window_start)
    if not recurring:
        logger.info("No recurring activities — nothing to check.")
        return

    alert_lines: list[str] = []
    alerted_types: list[str] = []
    lapsed_since: list[date] = []
    for description, entries in recurring.items():
        active_days = {_entry_local_date(e["start"]) for e in entries}
        last_date = max(active_days)
        interval = window_weeks * 7 / len(active_days)
        gap_threshold = max(_MIN_GAP_DAYS, round(2 * interval))
        days_since = (today - last_date).days
        if days_since < gap_threshold:
            logger.info("%s: %d days since last time (lapse at %d) — OK", description, days_since, gap_threshold)
            continue
        alert_type = f"session_gap:{description}"
        if archive.is_on_cooldown(alert_type, 0, cooldown_days):
            logger.info("Skipping %s — on cooldown (%dd)", description, cooldown_days)
            continue

        usual = "every day" if round(interval) <= 1 else f"every {round(interval)} days"
        line = f"{description}: {days_since} days since last time ({last_date:%a} {_day_month(last_date)}), usually {usual}"
        logger.info("LAPSED: %s", line)
        alert_lines.append(line)
        alerted_types.append(alert_type)
        lapsed_since.append(last_date)

    if not alert_lines:
        return

    # Nothing tracked at all since the lapses began is more likely a stuck
    # dashboard sync (alerts only read its archive) or a break from tracking
    # than every routine lapsing at once — say that instead of listing them.
    newest_entry = archive.get_last_entry_time()
    if newest_entry:
        newest_date = _entry_local_date(newest_entry)
        if newest_date <= max(lapsed_since):
            alert_lines = [
                f"⚠ Nothing tracked at all since {_day_month(newest_date)} "
                f"({len(alert_lines)} routine{'s' if len(alert_lines) != 1 else ''} overdue)"
                " — is the dashboard sync stuck?"
            ]

    if _send_alert(apprise_url, "Lapsed routines", alert_lines):
        for alert_type in alerted_types:
            archive.mark_alerted(alert_type, [0])


_ENTRYPOINTS = {
    "--digest": run_weekly_digest,
    "--monthly": run_monthly_report,
    "--session-gap": run_session_gap_alerts,
}


if __name__ == "__main__":
    flags = [a for a in sys.argv[1:] if a in _ENTRYPOINTS]
    if len(flags) != 1 or len(sys.argv) != 2:
        sys.exit(f"usage: python -m src.alerts {{{'|'.join(_ENTRYPOINTS)}}}")
    if _is_dry_run():
        logger.info("DRY RUN mode enabled — alerts will be logged but not sent")
    _ENTRYPOINTS[flags[0]]()
