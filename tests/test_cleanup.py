"""Unit tests for src.cleanup: the spurious-entry rules, apply/restore and the write client."""

import json
from datetime import date
from unittest.mock import Mock, patch

import pytest
import requests

import src.cleanup as cleanup
from src.cleanup import FRAGMENT, LONG, STUB, BackupLog, CleanupThresholds, covered_secs, find_spurious
from src.toggl import RATE_LIMIT_RETRY_DELAY_SECONDS, TogglArchive, TogglWriteClient

THRESHOLDS = CleanupThresholds()


def _e(entry_id: int, start: str, duration: int, description: str = "Yoga") -> dict:
    return {"id": entry_id, "description": description, "start": start, "duration": duration}


class TestCoveredSecs:
    def test_leaves_gaps_out(self):
        assert covered_secs([_e(1, "2026-06-01T10:00:00+02:00", 600), _e(2, "2026-06-01T10:20:00+02:00", 600)]) == 1200

    def test_counts_overlaps_once(self):
        assert covered_secs([_e(1, "2026-06-01T10:00:00+02:00", 600), _e(2, "2026-06-01T10:05:00+02:00", 600)]) == 900

    def test_duplicate_counts_once(self):
        assert covered_secs([_e(1, "2026-06-01T10:00:00+02:00", 600), _e(2, "2026-06-01T10:00:00+02:00", 600)]) == 600


class TestFindSpurious:
    def _find(self, entries: list[dict], median: int = 3600) -> list:
        return find_spurious({"Yoga": entries}, {"Yoga": median}, THRESHOLDS)

    def test_stub_below_a_tenth_of_the_median(self):
        (c,) = self._find([_e(1, "2026-06-01T10:00:00+02:00", 300)])
        assert (c.rule, [e["id"] for e in c.entries]) == (STUB, [1])

    def test_stub_below_the_absolute_minimum_for_short_activities(self):
        (c,) = self._find([_e(1, "2026-06-01T10:00:00+02:00", 45)], median=300)
        assert c.rule == STUB

    def test_usual_entries_are_left_alone(self):
        assert self._find([_e(1, "2026-06-01T10:00:00+02:00", 3000), _e(2, "2026-06-02T10:00:00+02:00", 4000)]) == []

    def test_forgotten_timer_is_reported(self):
        (c,) = self._find([_e(1, "2026-06-01T10:00:00+02:00", 4 * 3600)])
        assert c.rule == LONG

    def test_long_needs_the_absolute_minimum_too(self):
        # 3x a 10-minute median, but well under 2 hours: a long drive, not a forgotten timer
        assert self._find([_e(1, "2026-06-01T10:00:00+02:00", 40 * 60)], median=600) == []

    def test_restart_is_merged_by_covered_time(self):
        (c,) = self._find([_e(1, "2026-06-01T10:00:00+02:00", 1800), _e(2, "2026-06-01T10:31:00+02:00", 1500)])
        assert c.rule == FRAGMENT
        assert [e["id"] for e in c.entries] == [1, 2]
        assert c.merged_secs == 3300

    def test_duplicate_merges_away(self):
        (c,) = self._find([_e(1, "2026-06-01T10:00:00+02:00", 1800), _e(2, "2026-06-01T10:00:00+02:00", 1800)])
        assert (c.rule, c.merged_secs) == (FRAGMENT, 1800)

    def test_no_merge_across_a_longer_pause(self):
        assert self._find([_e(1, "2026-06-01T10:00:00+02:00", 1800), _e(2, "2026-06-01T10:40:00+02:00", 1500)]) == []

    def test_no_merge_across_days(self):
        entries = [_e(1, "2026-06-01T23:00:00+02:00", 3540), _e(2, "2026-06-02T00:00:00+02:00", 1800)]
        assert self._find(entries) == []

    def test_pause_capped_at_the_median(self):
        thresholds = CleanupThresholds(fragment_gap_minutes=30)
        entries = [_e(1, "2026-06-01T10:00:00+02:00", 300), _e(2, "2026-06-01T10:15:00+02:00", 300)]
        # 10-minute pause between two usual 5-minute sessions: separate sessions
        assert find_spurious({"Yoga": entries}, {"Yoga": 300}, thresholds) == []

    def test_stub_inside_a_restart_is_deleted_not_merged(self):
        entries = [
            _e(1, "2026-06-01T10:00:00+02:00", 1800),
            _e(2, "2026-06-01T10:30:30+02:00", 10),
            _e(3, "2026-06-01T10:31:00+02:00", 1500),
        ]
        stub, fragment = self._find(entries)
        assert (stub.rule, [e["id"] for e in stub.entries]) == (STUB, [2])
        assert (fragment.rule, [e["id"] for e in fragment.entries]) == (FRAGMENT, [1, 3])

    def test_forgotten_timer_is_never_merged(self):
        entries = [_e(1, "2026-06-01T10:00:00+02:00", 1800), _e(2, "2026-06-01T10:31:00+02:00", 5 * 3600)]
        assert [c.rule for c in self._find(entries)] == [LONG]

    def test_description_without_median_is_skipped(self):
        assert find_spurious({"Yoga": [_e(1, "2026-06-01T10:00:00+02:00", 5)]}, {}, THRESHOLDS) == []


class TestThresholdsFromEnv:
    def test_overrides_and_bad_values(self, monkeypatch):
        monkeypatch.setenv("CLEANUP_FRAGMENT_GAP_MINUTES", "5")
        monkeypatch.setenv("CLEANUP_STUB_RATIO", "not-a-number")
        t = CleanupThresholds.from_env()
        assert t.fragment_gap_minutes == 5
        assert t.stub_ratio == CleanupThresholds().stub_ratio


def _row(entry_id: int, day: str, duration: int, description: str = "Yoga", hour: int = 10) -> dict:
    return {
        "id": entry_id,
        "project_id": 7,
        "description": description,
        "start": f"{day}T{hour:02d}:00:00+02:00",
        "stop": "x",
        "duration": duration,
    }


def _weekly_yoga(start_id: int = 100) -> list[dict]:
    """A recurring 1-hour Yoga session every week since April, so the median is 3600."""
    days = [date.fromordinal(date(2026, 4, 6).toordinal() + 7 * i).isoformat() for i in range(12)]
    return [_row(start_id + i, d, 3600) for i, d in enumerate(days)]


class TestCollect:
    def test_recurring_entries_and_medians_without_today(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TOGGL_IGNORED_DESCRIPTIONS", raising=False)
        archive = TogglArchive(str(tmp_path / "toggl.db"))
        today = date(2026, 6, 29)
        archive.upsert([*_weekly_yoga(), _row(1, "2026-06-28", 20), _row(2, "2026-06-29", 20)])

        entries, medians = cleanup.collect(archive, today, date(2026, 6, 1), None)

        assert medians == {"Yoga": 3600}
        assert [e["id"] for e in entries["Yoga"] if e["start"] >= "2026-06-28"] == [1]

    def test_description_filter(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TOGGL_IGNORED_DESCRIPTIONS", raising=False)
        archive = TogglArchive(str(tmp_path / "toggl.db"))
        archive.upsert(_weekly_yoga())
        entries, _ = cleanup.collect(archive, date(2026, 6, 29), date(2026, 6, 1), "spanish")
        assert entries == {}


class FakeWriter:
    """Records calls; get_entry answers from `remote` like Toggl would (None once deleted)."""

    def __init__(self, remote: dict[int, dict], log: BackupLog) -> None:
        self.remote = remote
        self.log = log
        self.calls: list[tuple] = []
        self.next_id = 9000

    def get_entry(self, entry_id):
        return self.remote.get(entry_id)

    def delete_entry(self, entry_id):
        # the backup must already hold this entry when Toggl deletes it
        assert any(r["entry"]["id"] == entry_id for r in self._logged())
        self.calls.append(("delete", entry_id))
        del self.remote[entry_id]

    def update_duration(self, entry_id, start, duration):
        assert any(r["entry"]["id"] == entry_id for r in self._logged())
        self.calls.append(("update", entry_id, duration))

    def create_entry(self, original):
        self.next_id += 1
        self.calls.append(("create", original["id"]))
        return {**original, "id": self.next_id}

    def _logged(self) -> list[dict]:
        with open(self.log.path) as f:
            return [json.loads(line) for line in f]


def _remote(row: dict) -> dict:
    """The same entry as Toggl's GET /me/time_entries/{id} returns it (UTC start)."""
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "description": row["description"],
        "start": "x",
        "duration": row["duration"],
        "tag_ids": [3],
    }


class TestApplyAndRestore:
    def _setup(self, tmp_path, rows):
        archive = TogglArchive(str(tmp_path / "toggl.db"))
        archive.upsert(rows)
        log = BackupLog(str(tmp_path / "backup.jsonl"))
        writer = FakeWriter({r["id"]: _remote(r) for r in rows}, log)
        return archive, log, writer

    def _ids(self, archive) -> list[int]:
        return sorted(e["id"] for e in archive.get_described_entries(since="2000-01-01"))

    def test_stub_and_fragment_then_restore(self, tmp_path):
        rows = [_row(1, "2026-06-01", 1800), _row(2, "2026-06-01", 1500, hour=11), _row(3, "2026-06-02", 10)]
        archive, log, writer = self._setup(tmp_path, rows)
        candidates = [
            cleanup.Candidate(STUB, "Yoga", 3600, (rows[2],)),
            cleanup.Candidate(FRAGMENT, "Yoga", 3600, (rows[0], rows[1]), 3300),
            cleanup.Candidate(LONG, "Yoga", 3600, (rows[0],)),
        ]

        assert cleanup.apply(candidates, writer, archive, log, "run1") == 2

        assert writer.calls == [("delete", 3), ("update", 1, 3300), ("delete", 2)]
        assert self._ids(archive) == [1]
        assert archive.get_entries(7) == [{"start": "2026-06-01T10:00:00+02:00", "duration": 3300}]

        writer.calls.clear()
        assert cleanup.restore("run1", writer, archive, log) == 3

        assert writer.calls == [("create", 2), ("update", 1, 1800), ("create", 3)]
        restored = {(e["start"], e["duration"]) for e in archive.get_described_entries(since="2000-01-01")}
        assert restored == {(r["start"], r["duration"]) for r in rows}
        with pytest.raises(SystemExit, match="already restored"):
            cleanup.restore("run1", writer, archive, log)

    def test_entry_already_gone_from_toggl(self, tmp_path):
        rows = [_row(1, "2026-06-02", 10)]
        archive, log, writer = self._setup(tmp_path, rows)
        writer.remote.clear()

        cleanup.apply([cleanup.Candidate(STUB, "Yoga", 3600, (rows[0],))], writer, archive, log, "run1")

        assert writer.calls == []
        assert self._ids(archive) == []

    def test_restore_unknown_run(self, tmp_path):
        archive, log, writer = self._setup(tmp_path, [])
        with pytest.raises(SystemExit, match="No changes"):
            cleanup.restore("nope", writer, archive, log)


class TestMain:
    def test_report_only_never_writes(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("TOGGL_IGNORED_DESCRIPTIONS", raising=False)
        db_path = str(tmp_path / "toggl.db")
        monkeypatch.setenv("TOGGL_DB_PATH", db_path)
        TogglArchive(db_path).upsert([*_weekly_yoga(), _row(1, "2026-06-28", 20)])
        monkeypatch.setattr(cleanup, "date", Mock(today=Mock(return_value=date(2026, 6, 29)), wraps=date))
        with patch.object(cleanup, "TogglWriteClient", side_effect=AssertionError("no writes in report mode")):
            cleanup.main(["--since", "2026-06-01"])
        out = capsys.readouterr().out
        assert "Stubs, to delete (1)" in out
        assert "Report only" in out


def _response(status: int, body: dict | None = None, headers: dict | None = None) -> Mock:
    response = Mock(status_code=status, headers=headers or {}, content=json.dumps(body).encode() if body else b"")
    response.json.return_value = body
    if status >= 400:
        response.raise_for_status.side_effect = requests.exceptions.HTTPError(response=response)
    return response


class TestTogglWriteClient:
    def test_update_keeps_start_and_sets_matching_stop(self):
        client = TogglWriteClient("token", 123)
        with patch("requests.request", return_value=_response(200, {"id": 1})) as req:
            client.update_duration(1, "2026-06-01T08:00:00+00:00", 3300)
        method, url = req.call_args.args
        assert (method, url) == ("PUT", "https://api.track.toggl.com/api/v9/workspaces/123/time_entries/1")
        assert req.call_args.kwargs["json"] == {
            "start": "2026-06-01T08:00:00Z",
            "stop": "2026-06-01T08:55:00Z",
            "duration": 3300,
        }

    def test_create_copies_the_original(self):
        client = TogglWriteClient("token", 123)
        original = {
            "id": 1,
            "project_id": 7,
            "task_id": None,
            "description": "Yoga",
            "start": "2026-06-01T10:00:00+02:00",
            "duration": 600,
            "billable": False,
            "tag_ids": [3],
        }
        with patch("requests.request", return_value=_response(200, {"id": 2})) as req:
            assert client.create_entry(original)["id"] == 2
        assert req.call_args.kwargs["json"] == {
            "created_with": "toggl-insights",
            "workspace_id": 123,
            "start": "2026-06-01T08:00:00Z",
            "duration": 600,
            "project_id": 7,
            "description": "Yoga",
            "billable": False,
            "tag_ids": [3],
        }

    def test_get_missing_entry_is_none(self):
        with patch("requests.request", return_value=_response(404)):
            assert TogglWriteClient("token", 123).get_entry(1) is None

    def test_delete_with_empty_body(self):
        with patch("requests.request", return_value=_response(200)) as req:
            TogglWriteClient("token", 123).delete_entry(1)
        assert req.call_args.args[0] == "DELETE"

    def test_waits_out_the_quota_then_retries(self):
        with patch("requests.request", side_effect=[_response(402), _response(200, {"id": 1})]):
            with patch("time.sleep") as sleep:
                assert TogglWriteClient("token", 123).get_entry(1) == {"id": 1}
        sleep.assert_called_once_with(RATE_LIMIT_RETRY_DELAY_SECONDS)

    def test_other_errors_raise(self):
        with patch("requests.request", return_value=_response(500)):
            with pytest.raises(requests.exceptions.HTTPError):
                TogglWriteClient("token", 123).delete_entry(1)
