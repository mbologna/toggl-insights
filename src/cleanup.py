"""Find, and optionally fix, spurious entries of recurring activities in Toggl.

Usage:
    python -m src.cleanup                        # report only: list what would change
    python -m src.cleanup --apply [--limit N]    # change it in Toggl and the archive
    python -m src.cleanup --restore RUN_ID       # undo an --apply run
    python -m src.cleanup --scheduled            # weekly CronJob: apply within a budget, report via Apprise
    options: --since YYYY-MM-DD (default: 90 days ago), --description WORDS,
             --rule stub|daily|long (only that rule)

Three rules, each relative to the description's own median entry length over
the last 12 weeks (thresholds in CleanupThresholds, overridable via CLEANUP_*):

- stub: far shorter than usual (a timer started by mistake) -> deleted
- daily: more than one entry on a day, for an activity that happens once a day
  (a timer stopped and restarted) -> merged into the first entry, whose
  duration becomes the time the entries cover (gaps excluded, overlaps counted
  once). An activity counts as once a day when nearly all its days in the
  last 12 weeks have a single entry. Activities that can repeat in a day (a
  drive, a shop) are never merged.
- long: far longer than usual (a timer left running) -> reported only, since
  the real stop time is unknown; fix it in Toggl

--scheduled applies stubs and daily merges, at most CLEANUP_MAX_WRITES Toggl
writes per run (the free plan's quota is 30 workspace requests an hour, shared
with the sync), and sends what it changed, plus forgotten timers from the last
week and what the dashboard's daily history check against Toggl corrected, to
APPRISE_URL. It stays silent when there is nothing to say.

Every --apply change is preceded by a copy of the entry, as Toggl returns it,
appended to the backup log (CLEANUP_BACKUP_PATH, default next to the
archive); --restore replays the log. Toggl has no trash, so the log is the only
undo.
"""

import argparse
import json
import logging
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from dotenv import load_dotenv

# Allow running as `python src/cleanup.py` from project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.alerts import _send_alert  # noqa: E402
from src.toggl import (  # noqa: E402
    REPORT_ROUTINES,
    TogglArchive,
    TogglQuotaExhausted,
    TogglReportsClient,
    TogglWriteClient,
    _parse_iso,
    format_duration,
    match_descriptions,
    recurring_descriptions,
    session_stats,
)

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

STUB = "stub"
DAILY = "daily"
LONG = "long"
_MEDIAN_WEEKS = 12
_SCAN_DAYS = 90
_DEFAULT_MAX_WRITES = 12
# Workspace requests to leave unused, for the hourly sync running meanwhile.
_QUOTA_MARGIN = 2
_REVIEW_DAYS = 7
_MESSAGE_MAX_CHARS = 3500  # under Telegram's 4096 limit, with room for the title


@dataclass(frozen=True)
class CleanupThresholds:
    stub_ratio: float = 0.10
    stub_min_secs: int = 60
    long_ratio: float = 3.0
    long_min_secs: int = 7200
    # Once a day: at least daily_min_days days with entries in the median window,
    # and at least daily_ratio of them with a single entry (stubs not counted).
    daily_ratio: float = 0.9
    daily_min_days: int = 8

    @classmethod
    def from_env(cls) -> CleanupThresholds:
        defaults = cls()

        def read(name: str, default: float) -> float:
            raw = os.environ.get(f"CLEANUP_{name.upper()}", "")
            try:
                return float(raw) if raw else default
            except ValueError:
                logger.warning("Invalid CLEANUP_%s value %r, using %s.", name.upper(), raw, default)
                return default

        return cls(
            stub_ratio=read("stub_ratio", defaults.stub_ratio),
            stub_min_secs=int(read("stub_min_secs", defaults.stub_min_secs)),
            long_ratio=read("long_ratio", defaults.long_ratio),
            long_min_secs=int(read("long_min_secs", defaults.long_min_secs)),
            daily_ratio=read("daily_ratio", defaults.daily_ratio),
            daily_min_days=int(read("daily_min_days", defaults.daily_min_days)),
        )

    def stub_below(self, median: int) -> float:
        return max(self.stub_min_secs, self.stub_ratio * median)

    def long_above(self, median: int) -> float:
        return max(self.long_min_secs, self.long_ratio * median)


@dataclass(frozen=True)
class Scan:
    """What collect() found: each recurring description's entries in the scanned
    days, its median entry length, and which descriptions happen once a day."""

    entries: dict[str, list[dict]]
    medians: dict[str, int]
    daily: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Candidate:
    """One finding. entries are archive rows (id, description, start, duration);
    for a daily merge the first one is kept and grows to merged_secs."""

    rule: str
    description: str
    median_secs: int
    entries: tuple[dict, ...] = field(default_factory=tuple)
    merged_secs: int = 0

    @property
    def day(self) -> str:
        return self.entries[0]["start"][:10]

    @property
    def writes(self) -> int:
        """Toggl write requests applying it takes: one delete per stub; for a daily
        merge, one update plus one delete per other entry."""
        return len(self.entries)


def _interval(entry: dict) -> tuple[datetime, datetime]:
    start = _parse_iso(entry["start"])
    return start, start + timedelta(seconds=entry["duration"])


def covered_secs(entries: list[dict]) -> int:
    """Seconds covered by the entries' intervals: gaps left out, overlaps counted once."""
    total = 0
    current: tuple[datetime, datetime] | None = None
    for start, stop in sorted(_interval(e) for e in entries):
        if current is None or start > current[1]:
            if current is not None:
                total += int((current[1] - current[0]).total_seconds())
            current = (start, stop)
        else:
            current = (current[0], max(current[1], stop))
    if current is not None:
        total += int((current[1] - current[0]).total_seconds())
    return total


def _by_day(entries: list[dict]) -> dict[str, list[dict]]:
    """Start-ordered entries grouped by local day (start_iso carries the local offset)."""
    days: dict[str, list[dict]] = {}
    for entry in sorted(entries, key=lambda e: _parse_iso(e["start"])):
        days.setdefault(entry["start"][:10], []).append(entry)
    return days


def is_once_a_day(entries: list[dict], median: int, thresholds: CleanupThresholds) -> bool:
    """True when nearly every day with (non-stub) entries has exactly one."""
    stub_below = thresholds.stub_below(median)
    counts = [sum(e["duration"] >= stub_below for e in day) for day in _by_day(entries).values()]
    counts = [c for c in counts if c]
    return len(counts) >= thresholds.daily_min_days and counts.count(1) >= thresholds.daily_ratio * len(counts)


def find_spurious(scan: Scan, thresholds: CleanupThresholds) -> list[Candidate]:
    """Apply the three rules (module docstring) to each description's entries."""
    candidates: list[Candidate] = []
    for description, entries in sorted(scan.entries.items()):
        median = scan.medians.get(description)
        if not median:
            continue
        stub_below, long_above = thresholds.stub_below(median), thresholds.long_above(median)
        for day in _by_day(entries).values():
            longs = [e for e in day if e["duration"] > long_above]
            rest = [e for e in day if e["duration"] <= long_above]
            candidates += [Candidate(LONG, description, median, (e,)) for e in longs]
            if description in scan.daily and len(rest) > 1:
                # Short pieces are part of the visit: merged in, not deleted.
                candidates.append(Candidate(DAILY, description, median, tuple(rest), covered_secs(rest)))
            else:
                candidates += [Candidate(STUB, description, median, (e,)) for e in rest if e["duration"] < stub_below]

    # Merges first: when the weekly budget runs out, they matter more to the stats than stubs.
    order = {DAILY: 0, STUB: 1, LONG: 2}
    return sorted(candidates, key=lambda c: (order[c.rule], c.entries[0]["start"]))


def collect(
    archive: TogglArchive,
    today: date,
    since: date,
    description_filter: str | None,
    thresholds: CleanupThresholds,
) -> Scan:
    """Recurring descriptions' entries from since to yesterday, their 12-week
    medians, and which of them happen once a day (judged on the same 12 weeks).

    Today is left out: a timer split today may still be running."""
    median_since = today - timedelta(weeks=_MEDIAN_WEEKS)
    recent = archive.get_described_entries(since=min(since, median_since).isoformat())
    descriptions = recurring_descriptions(recent, today, REPORT_ROUTINES)
    if description_filter:
        descriptions = match_descriptions(description_filter, descriptions)

    entries_by_description: dict[str, list[dict]] = {}
    medians: dict[str, int] = {}
    daily: set[str] = set()
    for description in descriptions:
        mine = [e for e in recent if e["description"] == description]
        dated = [(TogglReportsClient._entry_date(e), e) for e in mine]
        window = [e for d, e in dated if median_since < d < today]
        stats = session_stats(window)
        if stats["count"]:
            medians[description] = stats["median_secs"]
            entries_by_description[description] = [e for d, e in dated if since <= d < today]
            if is_once_a_day(window, stats["median_secs"], thresholds):
                daily.add(description)
    return Scan(entries_by_description, medians, frozenset(daily))


def _gaps(entries: tuple[dict, ...]) -> list[int]:
    """Seconds from each entry's stop to the next one's start (negative: overlap)."""
    intervals = [_interval(e) for e in entries]
    return [int((nxt[0] - cur[1]).total_seconds()) for cur, nxt in zip(intervals, intervals[1:], strict=False)]


def _secs(seconds: int) -> str:
    return f"{seconds}s" if seconds < 60 else format_duration(seconds)


def format_report(candidates: list[Candidate]) -> str:
    sections = {
        DAILY: "Same-day entries of a once-a-day activity, to merge into the first",
        STUB: "Stubs, to delete",
        LONG: "Forgotten timers, to fix by hand in Toggl (report only)",
    }
    lines: list[str] = []
    for rule, title in sections.items():
        found = [c for c in candidates if c.rule == rule]
        if not found:
            continue
        lines.append(f"{title} ({len(found)}):")
        for c in found:
            times = " + ".join(f"{e['start'][11:16]} {_secs(e['duration'])}" for e in c.entries)
            result = ""
            if rule == DAILY:
                gaps = ", ".join(_secs(g) if g >= 0 else f"overlap {_secs(-g)}" for g in _gaps(c.entries))
                result = f" → {_secs(c.merged_secs)} (gaps: {gaps})"
            ids = ", ".join(f"#{e['id']}" for e in c.entries)
            lines.append(f"  {c.day}  {c.description}: {times}{result}  (usual {_secs(c.median_secs)}; {ids})")
    return "\n".join(lines) if lines else "Nothing to clean up."


class BackupLog:
    """Append-only JSON lines: one record per Toggl change, written before the change."""

    def __init__(self, path: str) -> None:
        self.path = path

    def append(self, record: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def records(self, run_id: str) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as f:
            return [r for r in (json.loads(line) for line in f if line.strip()) if r["run_id"] == run_id]


def apply(
    candidates: list[Candidate],
    writer: TogglWriteClient,
    archive: TogglArchive,
    log: BackupLog,
    run_id: str,
    guard_quota: bool = False,
) -> Iterator[Candidate]:
    """Carry out the stub and daily candidates, yielding each one once applied.

    Each change is logged (with Toggl's full copy of the entry) before it is
    made, so --restore can undo a run even if it stopped half-way. With
    guard_quota, a candidate is only started when the quota left covers all of
    it (raising TogglQuotaExhausted otherwise): a daily merge cut off half-way
    would leave the grown entry next to pieces it already counts."""
    for c in candidates:
        if c.rule == LONG:
            continue
        if guard_quota and writer.quota_remaining is not None and writer.quota_remaining < c.writes + _QUOTA_MARGIN:
            raise TogglQuotaExhausted(f"only {writer.quota_remaining} Toggl requests left this hour")
        keep, drop = (c.entries[0], c.entries[1:]) if c.rule == DAILY else (None, c.entries)
        if keep is not None:
            original = writer.get_entry(keep["id"])
            if original is None:
                # Already gone from Toggl: the archive was stale. Re-run to re-evaluate the rest.
                archive.delete_ids([keep["id"]])
                logger.warning("Skipped %s on %s: #%d is no longer in Toggl", c.rule, c.day, keep["id"])
                continue
            log.append(
                {
                    "run_id": run_id,
                    "action": "update",
                    "at": datetime.now(tz=UTC).isoformat(),
                    "entry": original,
                    "archived": keep,
                }
            )
            writer.update_duration(keep["id"], original["start"], c.merged_secs)
            archive.set_duration(keep["id"], c.merged_secs)
        for entry in drop:
            original = writer.get_entry(entry["id"])
            if original is None:
                archive.delete_ids([entry["id"]])
                logger.info("#%d was already deleted in Toggl", entry["id"])
                continue
            log.append(
                {
                    "run_id": run_id,
                    "action": "delete",
                    "at": datetime.now(tz=UTC).isoformat(),
                    "entry": original,
                    "archived": entry,
                }
            )
            writer.delete_entry(entry["id"])
            archive.delete_ids([entry["id"]])
        logger.info("Applied %s on %s: %s", c.rule, c.day, c.description)
        yield c


def restore(run_id: str, writer: TogglWriteClient, archive: TogglArchive, log: BackupLog) -> int:
    """Undo an --apply run, newest change first. Deleted entries come back with new ids.

    A record is written before its change, so a run that failed on a delete
    logged one that never happened: entries still in Toggl are skipped, not
    duplicated."""
    records = log.records(run_id)
    if any(r["action"] == "restored" for r in records):
        raise SystemExit(f"Run {run_id} was already restored.")
    changes = [r for r in records if r["action"] in ("update", "delete")]
    if not changes:
        raise SystemExit(f"No changes logged for run {run_id} in {log.path}.")

    for record in reversed(changes):
        original, archived = record["entry"], record["archived"]
        if record["action"] == "update":
            writer.update_duration(original["id"], original["start"], original["duration"])
            archive.set_duration(original["id"], original["duration"])
        elif writer.get_entry(original["id"]) is not None:
            logger.info("#%d is still in Toggl (its delete never happened); skipped", original["id"])
        else:
            created = writer.create_entry(original)
            archive.upsert(
                [
                    {
                        "id": created["id"],
                        "project_id": original.get("project_id"),
                        "description": archived["description"],
                        "start": archived["start"],
                        "stop": "x",
                        "duration": archived["duration"],
                    }
                ]
            )
    log.append({"run_id": run_id, "action": "restored", "at": datetime.now(tz=UTC).isoformat()})
    return len(changes)


def within_budget(candidates: list[Candidate], max_writes: int) -> tuple[list[Candidate], list[Candidate]]:
    """Split the applicable candidates into (this run, left for later) by write
    count, never splitting one. A single candidate over the whole budget still
    goes first, or it would never be applied."""
    now: list[Candidate] = []
    later: list[Candidate] = []
    used = 0
    for c in (c for c in candidates if c.rule != LONG):
        if not later and (used + c.writes <= max_writes or not now):
            now.append(c)
            used += c.writes
        else:
            later.append(c)
    return now, later


def _short_day(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{d.day} {d:%b}"


def history_line(archive: TogglArchive, now: datetime) -> tuple[str, bool]:
    """One line on the last week's daily history checks (main._check_history_if_due),
    and whether it is worth a message on its own: corrections or a problem are,
    a plain match only rides along."""
    checks = archive.history_checks_since(now - timedelta(days=_REVIEW_DAYS))
    complete = [c for c in checks if c["complete"]]
    if not complete:
        return f"History vs Toggl: no complete check in the last {_REVIEW_DAYS} days", True
    added, updated, removed, refused = (sum(c[k] for c in complete) for k in ("added", "updated", "removed", "refused"))
    if refused:
        line = (
            f"History vs Toggl: Toggl no longer returns {refused} archived entries, left in place "
            "(too many at once to trust): check the workspace"
        )
        return line, True
    if added or updated or removed:
        return f"History vs Toggl: {added} added, {updated} updated, {removed} removed this week", True
    return "History vs Toggl: matches", False


def format_message(
    applied: list[Candidate],
    review: list[Candidate],
    left: int,
    run_id: str,
    stopped: str | None,
    history: tuple[str, bool] | None = None,
) -> list[str]:
    """Body lines of the weekly message (empty: nothing worth sending)."""
    lines: list[str] = []
    if applied:
        lines.append(f"Fixed {len(applied)} · undo: python -m src.cleanup --restore {run_id}")
        for c in applied:
            what = (
                f"merged {len(c.entries)} entries → {_secs(c.merged_secs)}"
                if c.rule == DAILY
                else f"deleted a {_secs(c.entries[0]['duration'])} entry"
            )
            lines.append(f"• {_short_day(c.day)} {c.description}: {what}")
    if review:
        lines.append(f"To review (last {_REVIEW_DAYS} days):")
        lines += [
            f"• {_short_day(c.day)} {c.description}: {_secs(c.entries[0]['duration'])}, forgotten timer?"
            for c in review
        ]
    if stopped:
        lines.append(f"Stopped early: {stopped}")
    if left:
        lines.append(f"{left} left for next week (Toggl quota)")
    if history is not None:
        line, notable = history
        if notable or lines:
            lines.append(line)
    return _truncate(lines)


def _truncate(lines: list[str]) -> list[str]:
    kept: list[str] = []
    size = 0
    for i, line in enumerate(lines):
        if size + len(line) + 1 > _MESSAGE_MAX_CHARS:
            return [*kept, f"…and {len(lines) - i} more"]
        kept.append(line)
        size += len(line) + 1
    return kept


def run_scheduled(archive: TogglArchive, writer: TogglWriteClient, log: BackupLog, today: date) -> None:
    """The weekly CronJob: apply what fits the budget, then report it."""
    thresholds = CleanupThresholds.from_env()
    max_writes = int(os.environ.get("CLEANUP_MAX_WRITES") or _DEFAULT_MAX_WRITES)
    apprise_url = os.environ["APPRISE_URL"]
    candidates = find_spurious(
        collect(archive, today, today - timedelta(days=_SCAN_DAYS), None, thresholds), thresholds
    )
    review = [c for c in candidates if c.rule == LONG and c.day >= (today - timedelta(days=_REVIEW_DAYS)).isoformat()]
    now, later = within_budget(candidates, max_writes)

    run_id = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    applied: list[Candidate] = []
    stopped = None
    try:
        for c in apply(now, writer, archive, log, run_id, guard_quota=True):
            applied.append(c)
    except TogglQuotaExhausted as exc:
        stopped = str(exc)
    except Exception as exc:
        logger.exception("Cleanup run %s failed", run_id)
        undo = f" Undo what it changed with --restore {run_id}." if log.records(run_id) else ""
        _send_alert(apprise_url, "Toggl cleanup", [f"Stopped: {exc}.{undo}"])
        raise SystemExit(1) from None

    left = len(now) - len(applied) + len(later)
    lines = format_message(applied, review, left, run_id, stopped, history_line(archive, datetime.now(tz=UTC)))
    if not lines:
        logger.info("Nothing to report.")
        return
    _send_alert(apprise_url, "Toggl cleanup", lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", type=date.fromisoformat, help="first day to scan (default: 90 days ago)")
    parser.add_argument("--description", help="only matching descriptions (case, emoji and punctuation ignored)")
    parser.add_argument("--apply", action="store_true", help="change Toggl and the archive (default: report only)")
    parser.add_argument("--rule", choices=[STUB, DAILY, LONG], help="only findings of this rule")
    parser.add_argument("--limit", type=int, help="apply at most this many findings")
    parser.add_argument("--restore", metavar="RUN_ID", help="undo an --apply run")
    parser.add_argument("--scheduled", action="store_true", help="weekly CronJob mode (see above)")
    args = parser.parse_args(argv)

    db_path = os.environ.get("TOGGL_DB_PATH", "/data/toggl.db")
    archive = TogglArchive(db_path)
    log = BackupLog(
        os.environ.get("CLEANUP_BACKUP_PATH") or os.path.join(os.path.dirname(db_path), "cleanup-backup.jsonl")
    )

    def writer(wait_on_quota: bool = True) -> TogglWriteClient:
        return TogglWriteClient(os.environ["TOGGL_API_TOKEN"], int(os.environ["TOGGL_WORKSPACE_ID"]), wait_on_quota)

    if args.scheduled:
        run_scheduled(archive, writer(wait_on_quota=False), log, date.today())
        return

    if args.restore:
        count = restore(args.restore, writer(), archive, log)
        print(f"Restored {count} changes of run {args.restore}.")
        return

    today = date.today()
    thresholds = CleanupThresholds.from_env()
    scan = collect(archive, today, args.since or today - timedelta(days=90), args.description, thresholds)
    if scan.daily:
        print(f"Once a day (same-day entries get merged): {', '.join(sorted(scan.daily))}\n")
    candidates = find_spurious(scan, thresholds)
    if args.rule:
        candidates = [c for c in candidates if c.rule == args.rule]
    print(format_report(candidates))
    if not args.apply:
        if any(c.rule != LONG for c in candidates):
            print("\nReport only. Re-run with --apply to change Toggl.")
        return

    actionable = [c for c in candidates if c.rule != LONG][: args.limit]
    if not actionable:
        return
    run_id = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    print(f"\nApplying {len(actionable)} as run {run_id} (backup: {log.path})")
    try:
        applied = sum(1 for _ in apply(actionable, writer(), archive, log, run_id))
    except Exception:
        logger.exception("Stopped at the first failed change; undo the partial run with --restore %s", run_id)
        raise SystemExit(1) from None
    print(f"Applied {applied}. Undo with: python -m src.cleanup --restore {run_id}")


if __name__ == "__main__":
    main()
