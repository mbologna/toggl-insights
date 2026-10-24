"""Find, and optionally fix, spurious entries of recurring activities in Toggl.

Usage:
    python -m src.cleanup                        # report only: list what would change
    python -m src.cleanup --apply [--limit N]    # change it in Toggl and the archive
    python -m src.cleanup --restore RUN_ID       # undo an --apply run
    options: --since YYYY-MM-DD (default: 90 days ago), --description SUBSTRING,
             --rule stub|fragment (only that rule)

Three rules, each relative to the description's own median entry length over
the last 12 weeks (thresholds in CleanupThresholds, overridable via CLEANUP_*):

- stub: far shorter than usual (a timer started by mistake) -> deleted
- fragment: several entries of one activity on the same day with only a
  short pause between them (a timer stopped and restarted; default up to 2
  minutes, since longer pauses are often real separate trips) -> merged into the
  first, whose duration becomes the time the entries cover (gaps excluded,
  overlaps counted once, so a duplicated entry merges away)
- long: far longer than usual (a timer left running) -> reported only, since
  the real stop time is unknown; fix it in Toggl

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
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from dotenv import load_dotenv

# Allow running as `python src/cleanup.py` from project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.toggl import (  # noqa: E402
    REPORT_ROUTINES,
    TogglArchive,
    TogglReportsClient,
    TogglWriteClient,
    _parse_iso,
    format_duration,
    recurring_descriptions,
    session_stats,
)

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

STUB = "stub"
FRAGMENT = "fragment"
LONG = "long"
_MEDIAN_WEEKS = 12


@dataclass(frozen=True)
class CleanupThresholds:
    stub_ratio: float = 0.10
    stub_min_secs: int = 60
    fragment_gap_minutes: int = 2
    long_ratio: float = 3.0
    long_min_secs: int = 7200

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
            fragment_gap_minutes=int(read("fragment_gap_minutes", defaults.fragment_gap_minutes)),
            long_ratio=read("long_ratio", defaults.long_ratio),
            long_min_secs=int(read("long_min_secs", defaults.long_min_secs)),
        )


@dataclass(frozen=True)
class Candidate:
    """One finding. entries are archive rows (id, description, start, duration);
    for a fragment the first one is kept and grows to merged_secs."""

    rule: str
    description: str
    median_secs: int
    entries: tuple[dict, ...] = field(default_factory=tuple)
    merged_secs: int = 0

    @property
    def day(self) -> str:
        return self.entries[0]["start"][:10]


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


def _restart_groups(entries: list[dict], max_gap: timedelta) -> list[list[dict]]:
    """Split start-ordered entries into runs on the same local day where each
    starts at most max_gap after the run so far stopped."""
    groups: list[list[dict]] = []
    group_end: datetime | None = None
    for entry in entries:
        start, stop = _interval(entry)
        if (
            group_end is not None
            and entry["start"][:10] == groups[-1][0]["start"][:10]
            and start - group_end <= max_gap
        ):
            groups[-1].append(entry)
            group_end = max(group_end, stop)
        else:
            groups.append([entry])
            group_end = stop
    return groups


def find_spurious(
    entries_by_description: dict[str, list[dict]], medians: dict[str, int], thresholds: CleanupThresholds
) -> list[Candidate]:
    """Apply the three rules (module docstring) to each description's entries."""
    candidates: list[Candidate] = []
    for description, entries in sorted(entries_by_description.items()):
        median = medians.get(description)
        if not median:
            continue
        stub_below = max(thresholds.stub_min_secs, thresholds.stub_ratio * median)
        long_above = max(thresholds.long_min_secs, thresholds.long_ratio * median)
        # A pause longer than a usual session means a separate session, whatever the gap setting.
        max_gap = timedelta(seconds=min(thresholds.fragment_gap_minutes * 60, median))

        mergeable: list[dict] = []
        for entry in sorted(entries, key=lambda e: _parse_iso(e["start"])):
            if entry["duration"] < stub_below:
                candidates.append(Candidate(STUB, description, median, (entry,)))
            elif entry["duration"] > long_above:
                candidates.append(Candidate(LONG, description, median, (entry,)))
            else:
                mergeable.append(entry)

        for group in _restart_groups(mergeable, max_gap):
            if len(group) > 1:
                candidates.append(Candidate(FRAGMENT, description, median, tuple(group), covered_secs(group)))

    order = {STUB: 0, FRAGMENT: 1, LONG: 2}
    return sorted(candidates, key=lambda c: (order[c.rule], c.entries[0]["start"]))


def collect(
    archive: TogglArchive, today: date, since: date, description_filter: str | None
) -> tuple[dict[str, list[dict]], dict[str, int]]:
    """Recurring descriptions' entries from since to yesterday, plus their 12-week medians.

    Today is left out: a timer split today may still be running."""
    median_since = today - timedelta(weeks=_MEDIAN_WEEKS)
    recent = archive.get_described_entries(since=min(since, median_since).isoformat())
    needle = (description_filter or "").casefold()
    descriptions = [d for d in recurring_descriptions(recent, today, REPORT_ROUTINES) if needle in d.casefold()]

    entries_by_description: dict[str, list[dict]] = {}
    medians: dict[str, int] = {}
    for description in descriptions:
        mine = [e for e in recent if e["description"] == description]
        dated = [(TogglReportsClient._entry_date(e), e) for e in mine]
        stats = session_stats([e for d, e in dated if median_since < d < today])
        if stats["count"]:
            medians[description] = stats["median_secs"]
            entries_by_description[description] = [e for d, e in dated if since <= d < today]
    return entries_by_description, medians


def _gaps(entries: tuple[dict, ...]) -> list[int]:
    """Seconds from each entry's stop to the next one's start (negative: overlap)."""
    intervals = [_interval(e) for e in entries]
    return [int((nxt[0] - cur[1]).total_seconds()) for cur, nxt in zip(intervals, intervals[1:], strict=False)]


def _secs(seconds: int) -> str:
    return f"{seconds}s" if seconds < 60 else format_duration(seconds)


def format_report(candidates: list[Candidate]) -> str:
    sections = {
        STUB: "Stubs, to delete",
        FRAGMENT: "Fragments, to merge into the first entry",
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
            if rule == FRAGMENT:
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
    candidates: list[Candidate], writer: TogglWriteClient, archive: TogglArchive, log: BackupLog, run_id: str
) -> int:
    """Carry out the stub and fragment candidates; returns how many were applied.

    Each change is logged (with Toggl's full copy of the entry) before it is
    made, so --restore can undo a run even if it stopped half-way."""
    applied = 0
    for c in candidates:
        if c.rule == LONG:
            continue
        keep, drop = (c.entries[0], c.entries[1:]) if c.rule == FRAGMENT else (None, c.entries)
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
        applied += 1
        logger.info("Applied %s on %s: %s", c.rule, c.day, c.description)
    return applied


def restore(run_id: str, writer: TogglWriteClient, archive: TogglArchive, log: BackupLog) -> int:
    """Undo an --apply run, newest change first. Deleted entries come back with new ids."""
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", type=date.fromisoformat, help="first day to scan (default: 90 days ago)")
    parser.add_argument("--description", help="only descriptions containing this (case-insensitive)")
    parser.add_argument("--apply", action="store_true", help="change Toggl and the archive (default: report only)")
    parser.add_argument("--rule", choices=[STUB, FRAGMENT, LONG], help="only findings of this rule")
    parser.add_argument("--limit", type=int, help="apply at most this many findings")
    parser.add_argument("--restore", metavar="RUN_ID", help="undo an --apply run")
    args = parser.parse_args(argv)

    db_path = os.environ.get("TOGGL_DB_PATH", "/data/toggl.db")
    archive = TogglArchive(db_path)
    log = BackupLog(
        os.environ.get("CLEANUP_BACKUP_PATH") or os.path.join(os.path.dirname(db_path), "cleanup-backup.jsonl")
    )

    def writer() -> TogglWriteClient:
        return TogglWriteClient(os.environ["TOGGL_API_TOKEN"], int(os.environ["TOGGL_WORKSPACE_ID"]))

    if args.restore:
        count = restore(args.restore, writer(), archive, log)
        print(f"Restored {count} changes of run {args.restore}.")
        return

    today = date.today()
    entries, medians = collect(archive, today, args.since or today - timedelta(days=90), args.description)
    candidates = find_spurious(entries, medians, CleanupThresholds.from_env())
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
        applied = apply(actionable, writer(), archive, log, run_id)
    except Exception:
        logger.exception("Stopped at the first failed change; undo the partial run with --restore %s", run_id)
        raise SystemExit(1) from None
    print(f"Applied {applied}. Undo with: python -m src.cleanup --restore {run_id}")


if __name__ == "__main__":
    main()
