"""Unit tests for src.main's one-time historical backfill and HTTP endpoints."""

import sqlite3
from datetime import date, timedelta
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

import src.main as main
from src.toggl import TogglArchive, TogglReportsClient


class TestBackfillIfNeeded:
    """Test main._backfill_if_needed()."""

    def test_runs_when_archive_is_empty(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "toggl.db")
        monkeypatch.setattr(main, "_DB_PATH", db_path)
        monkeypatch.setattr(main, "_TOGGL_HISTORY_START_DATE", "2015-01-01")

        fake_client = Mock()
        fake_client.fetch_raw.return_value = [
            {"id": 1, "project_id": 1, "start": "2015-02-01T10:00:00Z", "stop": "x", "duration": 60},
        ]

        with patch.object(main, "_get_client", return_value=fake_client):
            main._backfill_if_needed()

        fake_client.fetch_raw.assert_called_once()
        start_date, end_date = fake_client.fetch_raw.call_args.args
        assert start_date == "2015-01-01"

        archive = TogglArchive(db_path)
        assert archive.get_earliest_entry_time() == "2015-02-01T10:00:00Z"

    def test_skipped_when_archive_already_reaches_start_date(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "toggl.db")
        monkeypatch.setattr(main, "_DB_PATH", db_path)
        monkeypatch.setattr(main, "_TOGGL_HISTORY_START_DATE", "2015-01-01")

        archive = TogglArchive(db_path)
        archive.upsert([{"id": 1, "project_id": 1, "start": "2014-06-01T10:00:00Z", "stop": "x", "duration": 60}])

        fake_client = Mock()
        with patch.object(main, "_get_client", return_value=fake_client):
            main._backfill_if_needed()

        fake_client.fetch_raw.assert_not_called()

    def test_runs_when_archive_does_not_reach_far_enough_back(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "toggl.db")
        monkeypatch.setattr(main, "_DB_PATH", db_path)
        monkeypatch.setattr(main, "_TOGGL_HISTORY_START_DATE", "2015-01-01")

        archive = TogglArchive(db_path)
        archive.upsert([{"id": 1, "project_id": 1, "start": "2020-06-01T10:00:00Z", "stop": "x", "duration": 60}])

        fake_client = Mock()
        fake_client.fetch_raw.return_value = []
        with patch.object(main, "_get_client", return_value=fake_client):
            main._backfill_if_needed()

        fake_client.fetch_raw.assert_called_once()

    def test_reruns_from_earliest_entry_when_descriptions_missing(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "toggl.db")
        monkeypatch.setattr(main, "_DB_PATH", db_path)
        monkeypatch.setattr(main, "_TOGGL_HISTORY_START_DATE", "2015-01-01")

        archive = TogglArchive(db_path)
        archive.upsert(
            [
                {"id": 1, "project_id": 1, "start": "2014-06-01T10:00:00Z", "stop": "x", "duration": 60},
                {"id": 2, "project_id": 1, "start": "2020-06-01T10:00:00Z", "stop": "x", "duration": 60},
            ]
        )
        with sqlite3.connect(db_path) as conn:
            conn.execute("UPDATE time_entries SET description = NULL")

        fake_client = Mock()
        fake_client.fetch_raw.return_value = [
            {
                "id": 2,
                "project_id": 1,
                "description": "Yoga",
                "start": "2020-06-01T10:00:00Z",
                "stop": "x",
                "duration": 60,
            },
        ]
        with patch.object(main, "_get_client", return_value=fake_client):
            main._backfill_if_needed()

        start_date, _ = fake_client.fetch_raw.call_args.args
        assert start_date == "2014-06-01"
        assert archive.get_description_entries("Yoga") == [{"start": "2020-06-01T10:00:00Z", "duration": 60}]
        # id 1 wasn't returned (deleted upstream) — cleared so it can't retrigger this forever
        assert not archive.has_missing_descriptions()

    def test_keeps_missing_descriptions_when_fetch_returns_nothing(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "toggl.db")
        monkeypatch.setattr(main, "_DB_PATH", db_path)
        monkeypatch.setattr(main, "_TOGGL_HISTORY_START_DATE", "2015-01-01")

        archive = TogglArchive(db_path)
        archive.upsert([{"id": 1, "project_id": 1, "start": "2014-06-01T10:00:00Z", "stop": "x", "duration": 60}])
        with sqlite3.connect(db_path) as conn:
            conn.execute("UPDATE time_entries SET description = NULL")

        fake_client = Mock()
        fake_client.fetch_raw.return_value = []
        with patch.object(main, "_get_client", return_value=fake_client):
            main._backfill_if_needed()

        assert archive.has_missing_descriptions()


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
