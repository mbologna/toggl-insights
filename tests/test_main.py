"""Unit tests for src.main's archive sync, daily history check and HTTP endpoints."""

import sqlite3
from datetime import date, datetime, timedelta
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

import src.main as main
from src.toggl import TogglArchive, TogglReportsClient

ROME = ZoneInfo("Europe/Rome")


def _entry(entry_id: int, start: str, description: str = "", duration: int = 60) -> dict:
    return {
        "id": entry_id,
        "project_id": 1,
        "description": description,
        "start": start,
        "stop": "x",
        "duration": duration,
    }


class TestCheckHistoryIfDue:
    """Test main._check_history_if_due(), the daily full-history comparison with Toggl."""

    NOW = datetime(2026, 10, 25, 4, 0, tzinfo=ROME)

    def _setup(self, tmp_path, monkeypatch, archived=(), fetched=(), complete=True):
        db_path = str(tmp_path / "toggl.db")
        monkeypatch.setattr(main, "_DB_PATH", db_path)
        monkeypatch.setattr(main, "_TOGGL_HISTORY_START_DATE", "2015-01-01")
        monkeypatch.delenv("ALERT_TIMEZONE", raising=False)
        archive = TogglArchive(db_path)
        archive.upsert(list(archived))
        fake_client = Mock()
        fake_client.fetch_window.return_value = (list(fetched), complete)
        monkeypatch.setattr(main, "_get_client", lambda: fake_client)
        return archive, fake_client

    def test_first_check_starts_from_the_configured_date(self, tmp_path, monkeypatch):
        archive, client = self._setup(tmp_path, monkeypatch, fetched=[_entry(1, "2016-02-01T10:00:00Z")])
        main._check_history_if_due(self.NOW)

        assert client.fetch_window.call_args.args == ("2015-01-01", "2026-10-25")
        assert archive.get_earliest_entry_time() == "2016-02-01T10:00:00Z"
        assert archive.get_meta(main._BACKFILL_FROM_KEY) == "2015-01-01"
        (check,) = archive.history_checks_since(self.NOW - timedelta(days=1))
        assert (check["complete"], check["added"]) == (1, 1)

    def test_once_per_local_day(self, tmp_path, monkeypatch):
        _, client = self._setup(tmp_path, monkeypatch, fetched=[_entry(1, "2016-02-01T10:00:00Z")])
        main._check_history_if_due(self.NOW)
        main._check_history_if_due(self.NOW + timedelta(hours=10))  # a restart later the same day
        assert client.fetch_window.call_count == 1

    def test_next_day_waits_for_the_night_hour(self, tmp_path, monkeypatch):
        _, client = self._setup(tmp_path, monkeypatch, fetched=[_entry(1, "2016-02-01T10:00:00Z")])
        with patch.object(main, "datetime", wraps=datetime) as fake_dt:
            fake_dt.now.return_value = self.NOW  # recorded as the last check
            main._check_history_if_due(self.NOW)
        main._check_history_if_due(datetime(2026, 10, 26, 2, 0, tzinfo=ROME))
        assert client.fetch_window.call_count == 1
        main._check_history_if_due(datetime(2026, 10, 26, 3, 0, tzinfo=ROME))
        assert client.fetch_window.call_count == 2

    def test_later_checks_start_the_day_before_the_earliest_entry(self, tmp_path, monkeypatch):
        archive, client = self._setup(tmp_path, monkeypatch, archived=[_entry(1, "2024-09-02T18:00:00+02:00")])
        archive.set_meta(main._BACKFILL_FROM_KEY, "2015-01-01")
        main._check_history_if_due(self.NOW)
        assert client.fetch_window.call_args.args == ("2024-09-01", "2026-10-25")

    def test_repairs_and_counts(self, tmp_path, monkeypatch):
        archived = [_entry(i, f"2025-0{i}-10T10:00:00+02:00") for i in range(1, 9)]
        archived[1]["start"] = "2025-02-10T08:00:00+00:00"  # old format, fixed by the fetch
        fetched = [_entry(i, f"2025-0{i}-10T10:00:00+02:00") for i in range(1, 8)]  # 8 deleted in Toggl
        fetched.append(_entry(20, "2025-09-10T10:00:00+02:00"))  # missing locally
        archive, _ = self._setup(tmp_path, monkeypatch, archived=archived, fetched=fetched)
        archive.set_meta(main._BACKFILL_FROM_KEY, "2015-01-01")

        main._check_history_if_due(self.NOW)

        (check,) = archive.history_checks_since(self.NOW - timedelta(days=1))
        assert {k: check[k] for k in ("added", "updated", "removed", "refused")} == {
            "added": 1,
            "updated": 1,
            "removed": 1,
            "refused": 0,
        }
        assert sorted(archive.rows_by_id()) == [1, 2, 3, 4, 5, 6, 7, 20]

    def test_incomplete_fetch_removes_nothing_and_retries(self, tmp_path, monkeypatch):
        archived = [_entry(1, "2025-01-10T10:00:00Z"), _entry(2, "2025-02-10T10:00:00Z")]
        archive, client = self._setup(
            tmp_path, monkeypatch, archived=archived, fetched=[_entry(3, "2025-03-10T10:00:00Z")], complete=False
        )
        main._check_history_if_due(self.NOW)
        main._check_history_if_due(self.NOW + timedelta(hours=1))

        assert client.fetch_window.call_count == 2
        assert sorted(archive.rows_by_id()) == [1, 2, 3]
        assert archive.get_meta(main._HISTORY_CHECKED_KEY) is None
        assert [c["complete"] for c in archive.history_checks_since(self.NOW - timedelta(days=1))] == [0, 0]

    def test_reruns_from_the_new_date_when_it_moves_earlier(self, tmp_path, monkeypatch):
        archive, client = self._setup(tmp_path, monkeypatch)
        archive.set_meta(main._BACKFILL_FROM_KEY, "2015-01-01")
        archive.set_meta(main._HISTORY_CHECKED_KEY, self.NOW.astimezone(main.UTC).isoformat())
        monkeypatch.setattr(main, "_TOGGL_HISTORY_START_DATE", "2010-01-01")
        main._check_history_if_due(self.NOW)
        assert client.fetch_window.call_args.args[0] == "2010-01-01"

    def test_missing_descriptions_are_filled_and_cleared(self, tmp_path, monkeypatch):
        archived = [_entry(1, "2014-06-01T10:00:00Z"), _entry(2, "2020-06-01T10:00:00Z")]
        archive, _ = self._setup(
            tmp_path, monkeypatch, archived=archived, fetched=[_entry(2, "2020-06-01T10:00:00Z", "Yoga")]
        )
        archive.set_meta(main._BACKFILL_FROM_KEY, "2015-01-01")
        archive.set_meta(main._HISTORY_CHECKED_KEY, self.NOW.astimezone(main.UTC).isoformat())
        with sqlite3.connect(main._DB_PATH) as conn:
            conn.execute("UPDATE time_entries SET description = NULL")

        main._check_history_if_due(self.NOW)

        assert archive.get_description_entries("Yoga") == [{"start": "2020-06-01T10:00:00Z", "duration": 60}]
        # id 1 wasn't returned (deleted upstream): cleared so it can't retrigger this forever
        assert not archive.has_missing_descriptions()

    def test_keeps_missing_descriptions_when_fetch_returns_nothing(self, tmp_path, monkeypatch):
        archive, _ = self._setup(tmp_path, monkeypatch, archived=[_entry(1, "2014-06-01T10:00:00Z")])
        with sqlite3.connect(main._DB_PATH) as conn:
            conn.execute("UPDATE time_entries SET description = NULL")
        main._check_history_if_due(self.NOW)
        assert archive.has_missing_descriptions()


def _archived(entry_id: int, day: str) -> dict:
    return {"id": entry_id, "project_id": 1, "start": f"{day}T10:00:00+02:00", "stop": "x", "duration": 600}


class TestSyncArchiveOnce:
    """The hourly sync also drops archived entries that Toggl no longer returns."""

    def _sync(self, tmp_path, monkeypatch, archived: list[dict], fetched: list[dict], complete: bool = True):
        db_path = str(tmp_path / "toggl.db")
        monkeypatch.setattr(main, "_DB_PATH", db_path)
        archive = TogglArchive(db_path)
        archive.upsert(archived)
        fake_client = Mock()
        fake_client.fetch_window.return_value = (fetched, complete)
        monkeypatch.setattr(main, "_get_client", lambda: fake_client)
        with patch.object(main, "date_range_for_granularity", return_value=("2026-07-26", "2026-10-24")):
            main._sync_archive_once()
        with sqlite3.connect(db_path) as conn:
            return sorted(r[0] for r in conn.execute("SELECT id FROM time_entries"))

    def test_removes_entries_deleted_in_toggl(self, tmp_path, monkeypatch):
        kept = [_archived(i, "2026-10-01") for i in range(1, 11)]
        ids = self._sync(tmp_path, monkeypatch, [*kept, _archived(99, "2026-10-02")], kept)
        assert ids == list(range(1, 11))

    def test_keeps_entries_outside_the_window_and_on_its_first_day(self, tmp_path, monkeypatch):
        kept = [_archived(i, "2026-10-01") for i in range(1, 11)]
        old = [_archived(50, "2025-01-01"), _archived(51, "2026-07-26")]
        ids = self._sync(tmp_path, monkeypatch, [*kept, *old], kept)
        assert ids == [*range(1, 11), 50, 51]

    def test_nothing_removed_after_an_incomplete_fetch(self, tmp_path, monkeypatch):
        archived = [_archived(i, "2026-10-01") for i in range(1, 11)]
        ids = self._sync(tmp_path, monkeypatch, archived, archived[:5], complete=False)
        assert ids == list(range(1, 11))

    def test_nothing_removed_after_an_empty_fetch(self, tmp_path, monkeypatch):
        archived = [_archived(i, "2026-10-01") for i in range(1, 11)]
        assert self._sync(tmp_path, monkeypatch, archived, []) == list(range(1, 11))

    def test_refuses_to_remove_a_large_share_of_the_window(self, tmp_path, monkeypatch):
        archived = [_archived(i, "2026-10-01") for i in range(1, 11)]
        ids = self._sync(tmp_path, monkeypatch, archived, archived[:7])
        assert ids == list(range(1, 11))

    def test_marks_sync_completed_even_when_incomplete(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "_last_sync_completed_at", None)
        self._sync(tmp_path, monkeypatch, [], [], complete=False)
        assert main._last_sync_completed_at is not None


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A TestClient wired to an isolated, empty archive with module caches reset."""
    db_path = str(tmp_path / "toggl.db")
    monkeypatch.setattr(main, "_DB_PATH", db_path)
    monkeypatch.setattr(main, "_api_cache", {})
    monkeypatch.setattr(main, "_heatmap_cache", None)
    monkeypatch.setattr(main, "_hours_cache", None)
    monkeypatch.setattr(main, "_sessions_cache", None)
    monkeypatch.setattr(main, "_client", None)
    monkeypatch.setattr(main, "_last_sync_completed_at", None)
    return TestClient(main.app)


@pytest.fixture
def toggl_client(monkeypatch):
    """A real TogglReportsClient with only its network-calling method stubbed,
    so aggregate_by_period's real logic still runs against archived entries."""
    fake = TogglReportsClient("fake-token", 123456)
    monkeypatch.setattr(
        fake,
        "get_projects",
        lambda: [{"id": 1, "name": "Project A", "color": "#4A90D9"}],
    )
    monkeypatch.setattr(main, "_get_client", lambda: fake)
    return fake


class TestHealth:
    def test_ok_when_db_reachable(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_503_when_db_unreachable(self, client, monkeypatch, tmp_path):
        # A directory (not a file) can't be opened as a sqlite db.
        monkeypatch.setattr(main, "_DB_PATH", str(tmp_path))
        resp = client.get("/health")
        assert resp.status_code == 503

    def test_ok_when_sync_has_never_completed(self, client, monkeypatch):
        # None means "still backfilling" (can legitimately take hours) — never stale.
        monkeypatch.setattr(main, "_last_sync_completed_at", None)
        assert client.get("/health").status_code == 200

    def test_ok_when_sync_is_recent(self, client, monkeypatch):
        monkeypatch.setattr(main, "_last_sync_completed_at", main.time.monotonic())
        assert client.get("/health").status_code == 200

    def test_ok_while_a_history_check_holds_up_the_sync(self, client, monkeypatch):
        monkeypatch.setattr(main, "_last_sync_completed_at", 0.0)
        monkeypatch.setattr(main, "_history_check_running", True)
        assert client.get("/health").status_code == 200

    def test_503_when_sync_is_stale(self, client, monkeypatch):
        stale_at = main.time.monotonic() - main._STALE_SYNC_AFTER_SECONDS - 1
        monkeypatch.setattr(main, "_last_sync_completed_at", stale_at)
        resp = client.get("/health")
        assert resp.status_code == 503
        assert "stale" in resp.json()["detail"]


class TestApiData:
    def test_shape(self, client, toggl_client):
        archive = TogglArchive(main._DB_PATH)
        archive.upsert([{"id": 1, "project_id": 1, "start": "2026-04-01T10:00:00Z", "stop": "x", "duration": 3600}])

        resp = client.get("/api/data")
        assert resp.status_code == 200
        data = resp.json()

        assert data["projects"] == [{"id": 1, "name": "Project A", "color": "#4A90D9"}]
        assert data["targets"] == {}
        assert data["yearly_goals"] == {}
        for granularity in main.GRANULARITIES:
            assert granularity in data
            assert isinstance(data[granularity], list)

        daily_row = next(row for row in data["daily"] if row["period"] == "2026-04-01")
        assert daily_row["1"] == 1.0


class TestApiHeatmap:
    def test_shape(self, client):
        archive = TogglArchive(main._DB_PATH)
        archive.upsert([{"id": 1, "project_id": 1, "start": "2026-04-01T10:00:00Z", "stop": "x", "duration": 3600}])

        resp = client.get("/api/heatmap")
        assert resp.status_code == 200
        data = resp.json()
        assert data["days"] == [{"date": "2026-04-01", "total": 1.0, "by_project": {"1": 1.0}}]
        assert data["last_entry_iso"] == "2026-04-01T10:00:00Z"
        assert data["daily_target"] is None


class TestApiHours:
    def test_shape(self, client):
        archive = TogglArchive(main._DB_PATH)
        archive.upsert([{"id": 1, "project_id": 1, "start": "2026-04-01T10:00:00Z", "stop": "x", "duration": 3600}])

        resp = client.get("/api/hours")
        assert resp.status_code == 200
        assert resp.json()["slots"]


class TestApiSessions:
    def test_empty_archive(self, client):
        resp = client.get("/api/sessions")
        assert resp.status_code == 200
        assert resp.json() == {"descriptions": []}

    def test_build_sessions_data(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TOGGL_IGNORED_DESCRIPTIONS", raising=False)
        archive = TogglArchive(str(tmp_path / "toggl.db"))
        yoga_days = ["2026-05-12", "2026-05-19", "2026-05-26", "2026-06-02", "2026-06-22", "2026-06-24"]
        rows = [
            {"id": i, "project_id": 1, "description": "Yoga", "start": f"{d}T18:00:00Z", "stop": "x", "duration": 3600}
            for i, d in enumerate(yoga_days, start=1)
        ]
        # older than 26 weeks: all-time only
        rows.append(
            {
                "id": 50,
                "project_id": 1,
                "description": "Yoga",
                "start": "2025-01-01T18:00:00Z",
                "stop": "x",
                "duration": 7200,
            }
        )
        # one-off: not recurring
        rows.append(
            {
                "id": 51,
                "project_id": 1,
                "description": "Documentary",
                "start": "2026-06-23T18:00:00Z",
                "stop": "x",
                "duration": 600,
            }
        )
        archive.upsert(rows)

        data = main._build_sessions_data(archive, date(2026, 6, 29))

        (yoga,) = data["descriptions"]
        assert yoga["description"] == "Yoga"
        assert yoga["last_4_weeks"]["count"] == 3
        assert yoga["last_12_weeks"]["count"] == 6
        assert yoga["all_time"]["count"] == 7
        assert yoga["weekly"][-1] == {"period": "2026-W26", "count": 2, "avg_secs": 3600}


class TestApiSessionsTypical:
    @staticmethod
    def _entry(entry_id: int, description: str, day: date, duration: int, hour: int = 13) -> dict:
        return {
            "id": entry_id,
            "project_id": 1,
            "description": description,
            "start": f"{day.isoformat()}T{hour:02d}:00:00+02:00",
            "stop": "x",
            "duration": duration,
        }

    def test_median_of_visits(self, tmp_path):
        archive = TogglArchive(str(tmp_path / "toggl.db"))
        today = date(2026, 10, 24)
        rows = [
            self._entry(1, "🧘 Yoga", date(2026, 10, 21), 9000),
            self._entry(2, "🧘 Yoga", date(2026, 10, 19), 8000),
            # restarted timer: same visit as entry 2
            self._entry(3, "🧘 Yoga", date(2026, 10, 19), 1000, hour=16),
            # short day: moves the mean, not the median
            self._entry(4, "🧘 Yoga", date(2026, 10, 2), 3000),
            # today, still in progress
            self._entry(5, "🧘 Yoga", today, 60),
            # before the 4-week window
            self._entry(6, "🧘 Yoga", date(2026, 9, 25), 100),
            self._entry(7, "Spanish", date(2026, 10, 21), 600),
        ]
        archive.upsert(rows)

        data = main._build_typical_session(archive, "YOGA", 4, today)

        assert data == {
            "description": "🧘 Yoga",
            "weeks": 4,
            "since": "2026-09-26",
            "until": "2026-10-23",
            "visits": 3,
            "minutes": 150,
            "median_secs": 9000,
            "avg_secs": 7000,
            "min_secs": 3000,
            "max_secs": 9000,
        }

    def test_window_start_is_inclusive(self, tmp_path):
        archive = TogglArchive(str(tmp_path / "toggl.db"))
        archive.upsert([self._entry(1, "Yoga", date(2026, 10, 17), 3600)])
        assert main._build_typical_session(archive, "yoga", 1, date(2026, 10, 24))["visits"] == 1
        with pytest.raises(main.HTTPException) as exc:
            main._build_typical_session(archive, "yoga", 1, date(2026, 10, 25))
        assert exc.value.status_code == 404

    def test_404_when_nothing_matches(self, client):
        resp = client.get("/api/sessions/typical", params={"description": "yoga"})
        assert resp.status_code == 404

    def test_409_when_ambiguous(self, client):
        yesterday = date.today() - timedelta(days=1)
        TogglArchive(main._DB_PATH).upsert(
            [self._entry(1, "Yoga flow", yesterday, 3600), self._entry(2, "Yoga stretch", yesterday, 3600)]
        )
        resp = client.get("/api/sessions/typical", params={"description": "yoga"})
        assert resp.status_code == 409
        assert resp.json()["detail"]["matches"] == ["Yoga flow", "Yoga stretch"]

    def test_full_title_without_emoji_or_punctuation(self, client):
        yesterday = date.today() - timedelta(days=1)
        TogglArchive(main._DB_PATH).upsert([self._entry(1, "🧘 Yoga + stretch", yesterday, 3600)])
        # "+" in a query string decodes to a space, as an iOS Shortcut would send it
        resp = client.get("/api/sessions/typical?description=yoga+stretch")
        assert resp.status_code == 200
        assert resp.json()["description"] == "🧘 Yoga + stretch"
        encoded = client.get("/api/sessions/typical", params={"description": "🧘 Yoga + stretch"})
        assert encoded.json()["description"] == "🧘 Yoga + stretch"

    def test_endpoint(self, client):
        yesterday = date.today() - timedelta(days=1)
        TogglArchive(main._DB_PATH).upsert([self._entry(1, "Yoga", yesterday, 5400)])
        resp = client.get("/api/sessions/typical", params={"description": "yoga", "weeks": 2})
        assert resp.status_code == 200
        assert resp.json()["minutes"] == 90

    def test_rejects_bad_weeks(self, client):
        assert client.get("/api/sessions/typical", params={"description": "yoga", "weeks": 0}).status_code == 422
        assert client.get("/api/sessions/typical", params={"description": "yoga", "weeks": 53}).status_code == 422


class TestDashboard:
    def test_serves_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "Toggl Insights" in resp.text
