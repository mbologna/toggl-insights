"""Unit tests for src.toggl's Reports API fetch, pagination, and retry/backoff behavior."""

import sqlite3
from datetime import date, timedelta
from unittest.mock import Mock, patch

import requests

from src.toggl import (
    RATE_LIMIT_MAX_RETRIES,
    RATE_LIMIT_RETRY_BUFFER_SECONDS,
    RATE_LIMIT_RETRY_DELAY_SECONDS,
    REPORT_ROUTINES,
    TogglArchive,
    TogglReportsClient,
    format_duration,
    load_ignored_descriptions,
    match_descriptions,
    recurring_descriptions,
    session_stats,
    sparkline,
    visit_stats,
)


def _http_error(status_code: int, headers: dict | None = None) -> requests.exceptions.HTTPError:
    response = Mock()
    response.status_code = status_code
    response.headers = headers or {}
    return requests.exceptions.HTTPError(response=response)


def _reports_response(rows: list[dict], next_headers: dict | None = None) -> Mock:
    resp = Mock()
    resp.json.return_value = rows
    resp.headers = next_headers or {}
    return resp


def _row(project_id: int, entries: list[dict]) -> dict:
    return {"project_id": project_id, "time_entries": entries}


class TestFetchChunkRetry:
    """Test TogglReportsClient._fetch_chunk() / _fetch_reports_page()'s 402 retry/backoff."""

    def _make_client(self) -> TogglReportsClient:
        return TogglReportsClient("token", 123)

    def test_success_flattens_entries(self):
        client = self._make_client()
        rows = [
            _row(456, [{"id": 1, "start": "2026-01-01T10:00:00Z", "stop": "2026-01-01T11:00:00Z", "seconds": 3600}])
        ]

        with patch("requests.post", return_value=_reports_response(rows)):
            result, complete = client._fetch_chunk("2026-01-01", "2026-01-31")

        assert complete
        assert result == [
            {
                "id": 1,
                "project_id": 456,
                "description": "",
                "start": "2026-01-01T10:00:00Z",
                "stop": "2026-01-01T11:00:00Z",
                "duration": 3600,
            }
        ]

    def test_paginates_using_next_headers(self):
        client = self._make_client()
        row1 = [
            _row(456, [{"id": 1, "start": "2026-01-01T10:00:00Z", "stop": "2026-01-01T11:00:00Z", "seconds": 3600}])
        ]
        row2 = [
            _row(456, [{"id": 2, "start": "2026-01-02T10:00:00Z", "stop": "2026-01-02T11:00:00Z", "seconds": 3600}])
        ]
        page1 = _reports_response(row1, {"x-next-id": "2", "x-next-row-number": "2", "x-next-timestamp": "1700000000"})
        page2 = _reports_response(row2)

        with patch("requests.post", side_effect=[page1, page2]) as mock_post:
            result, complete = client._fetch_chunk("2026-01-01", "2026-01-31")

        assert [e["id"] for e in result] == [1, 2]
        assert complete
        assert mock_post.call_count == 2
        second_body = mock_post.call_args_list[1].kwargs["json"]
        assert second_body["first_id"] == 2
        assert second_body["first_row_number"] == 2
        assert second_body["first_timestamp"] == 1700000000

    def test_402_retries_then_gives_up_returning_empty(self):
        """Persistent 402s are retried with backoff, then return [] rather than raising."""
        client = self._make_client()
        http_error = _http_error(402, headers={})

        with patch("requests.post") as mock_post:
            mock_post.return_value.raise_for_status.side_effect = http_error
            with patch("time.sleep") as mock_sleep:
                result, complete = client._fetch_chunk("2026-01-01", "2026-01-31")

        assert result == []
        assert not complete
        assert mock_post.call_count == RATE_LIMIT_MAX_RETRIES + 1
        assert mock_sleep.call_count == RATE_LIMIT_MAX_RETRIES

    def test_402_then_recovers(self):
        """A transient 402 followed by success returns the fetched entries."""
        client = self._make_client()
        http_error = _http_error(402, headers={})

        rate_limited_response = Mock()
        rate_limited_response.raise_for_status.side_effect = http_error
        success_response = _reports_response(
            [_row(456, [{"id": 42, "start": "2026-01-01T10:00:00Z", "stop": None, "seconds": 0}])]
        )

        with patch("requests.post", side_effect=[rate_limited_response, success_response]):
            with patch("time.sleep") as mock_sleep:
                result, complete = client._fetch_chunk("2026-01-01", "2026-01-31")

        assert [e["id"] for e in result] == [42]
        assert complete
        mock_sleep.assert_called_once_with(RATE_LIMIT_RETRY_DELAY_SECONDS)

    def test_402_honors_toggl_quota_reset_header(self):
        """When Toggl sends X-Toggl-Quota-Resets-In, wait that long instead of the fixed delay."""
        client = self._make_client()
        http_error = _http_error(402, headers={"x-toggl-quota-resets-in": "120"})

        rate_limited_response = Mock()
        rate_limited_response.raise_for_status.side_effect = http_error
        success_response = _reports_response([])

        with patch("requests.post", side_effect=[rate_limited_response, success_response]):
            with patch("time.sleep") as mock_sleep:
                result, complete = client._fetch_chunk("2026-01-01", "2026-01-31")

        assert result == []
        assert complete
        mock_sleep.assert_called_once_with(120 + RATE_LIMIT_RETRY_BUFFER_SECONDS)

    def test_non_402_http_error_returns_empty_without_retry(self):
        """A non-rate-limit HTTP error (e.g. 500) fails fast, no retry."""
        client = self._make_client()
        http_error = _http_error(500)

        with patch("requests.post") as mock_post:
            mock_post.return_value.raise_for_status.side_effect = http_error
            with patch("time.sleep") as mock_sleep:
                result, complete = client._fetch_chunk("2026-01-01", "2026-01-31")

        assert result == []
        assert not complete
        assert mock_post.call_count == 1
        mock_sleep.assert_not_called()

    def test_connection_error_returns_empty_without_retry(self):
        """A non-HTTP request exception (e.g. connection error) fails fast, no retry."""
        client = self._make_client()

        with patch("requests.post", side_effect=requests.exceptions.ConnectionError("boom")):
            with patch("time.sleep") as mock_sleep:
                result, complete = client._fetch_chunk("2026-01-01", "2026-01-31")

        assert result == []
        assert not complete
        mock_sleep.assert_not_called()

    def test_failed_later_page_keeps_earlier_pages_but_is_incomplete(self):
        client = self._make_client()
        page1 = _reports_response(
            [_row(456, [{"id": 1, "start": "2026-01-01T10:00:00Z", "stop": "2026-01-01T11:00:00Z", "seconds": 3600}])],
            {"x-next-id": "2", "x-next-row-number": "2", "x-next-timestamp": "1700000000"},
        )
        page2 = Mock()
        page2.raise_for_status.side_effect = _http_error(500)

        with patch("requests.post", side_effect=[page1, page2]):
            result, complete = client._fetch_chunk("2026-01-01", "2026-01-31")

        assert [e["id"] for e in result] == [1]
        assert not complete


class TestFetchEntriesChunking:
    """Test TogglReportsClient._fetch_entries() splits wide ranges into <=366-day chunks."""

    def test_splits_multi_year_range_into_chunks(self):
        client = TogglReportsClient("token", 123)

        with patch.object(client, "_fetch_chunk", return_value=([], True)) as mock_chunk:
            client._fetch_entries("2015-01-01", "2026-01-01")

        # 11 years at 366-day chunks: at least a handful of calls, each span <=366 days
        assert mock_chunk.call_count >= 10
        for call in mock_chunk.call_args_list:
            start, end = call.args
            from datetime import date

            span_days = (date.fromisoformat(end) - date.fromisoformat(start)).days
            assert span_days <= 366

    def test_incomplete_if_any_chunk_is(self):
        client = TogglReportsClient("token", 123)
        chunks = [([{"id": 1}], True), ([], False), ([{"id": 2}], True)]

        with patch.object(client, "_fetch_chunk", side_effect=chunks):
            entries, complete = client._fetch_entries("2024-01-01", "2026-12-31")

        assert [e["id"] for e in entries] == [1, 2]
        assert not complete


class TestTogglArchiveEarliestEntryTime:
    """Test TogglArchive.get_earliest_entry_time()."""

    def test_returns_none_when_empty(self, tmp_path):
        archive = TogglArchive(str(tmp_path / "toggl.db"))
        assert archive.get_earliest_entry_time() is None

    def test_returns_earliest_start_iso(self, tmp_path):
        archive = TogglArchive(str(tmp_path / "toggl.db"))
        archive.upsert(
            [
                {"id": 1, "project_id": 1, "start": "2026-03-01T10:00:00Z", "stop": "x", "duration": 60},
                {"id": 2, "project_id": 1, "start": "2025-01-01T10:00:00Z", "stop": "x", "duration": 60},
                {"id": 3, "project_id": 1, "start": "2026-06-01T10:00:00Z", "stop": "x", "duration": 60},
            ]
        )
        assert archive.get_earliest_entry_time() == "2025-01-01T10:00:00Z"


class TestTogglArchiveDailyTotals:
    """Test TogglArchive.get_daily_totals(), which GROUP BYs
    substr(start_iso, 1, 10) — the query the idx_day_project expression
    index targets."""

    def test_groups_by_day_and_project(self, tmp_path):
        archive = TogglArchive(str(tmp_path / "toggl.db"))
        archive.upsert(
            [
                {"id": 1, "project_id": 1, "start": "2026-04-01T09:00:00Z", "stop": "x", "duration": 3600},
                {"id": 2, "project_id": 2, "start": "2026-04-01T14:00:00Z", "stop": "x", "duration": 1800},
                {"id": 3, "project_id": 1, "start": "2026-04-02T09:00:00Z", "stop": "x", "duration": 7200},
            ]
        )

        days = archive.get_daily_totals()

        assert days == [
            {"date": "2026-04-01", "total": 1.5, "by_project": {"1": 1.0, "2": 0.5}},
            {"date": "2026-04-02", "total": 2.0, "by_project": {"1": 2.0}},
        ]

    def test_uses_the_day_project_expression_index(self, tmp_path):
        """The GROUP BY day, project_id must resolve to idx_day_project
        (substr(start_iso, 1, 10), project_id) rather than a full table scan
        — confirms the index actually matches the query, not just that it
        exists."""
        archive = TogglArchive(str(tmp_path / "toggl.db"))

        with sqlite3.connect(archive.db_path) as conn:
            plan = conn.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT substr(start_iso, 1, 10) AS day, project_id, SUM(duration_secs)
                FROM time_entries
                GROUP BY day, project_id
                ORDER BY day
                """
            ).fetchall()

        plan_text = " ".join(str(row) for row in plan)
        assert "idx_day_project" in plan_text


class TestFetchDescription:
    """The Reports API carries description at row level; it must reach every flattened entry."""

    def test_row_description_is_copied_to_entries(self):
        client = TogglReportsClient("token", 123)
        rows = [
            {
                "project_id": 456,
                "description": "Yoga",
                "time_entries": [
                    {"id": 1, "start": "2026-01-01T10:00:00Z", "stop": "2026-01-01T11:00:00Z", "seconds": 3600},
                    {"id": 2, "start": "2026-01-02T10:00:00Z", "stop": "2026-01-02T11:00:00Z", "seconds": 3600},
                ],
            }
        ]

        with patch("requests.post", return_value=_reports_response(rows)):
            result, complete = client._fetch_chunk("2026-01-01", "2026-01-31")

        assert [e["description"] for e in result] == ["Yoga", "Yoga"]


class TestTogglArchiveDescriptions:
    """Test the description column, its migration, and the re-backfill bookkeeping."""

    def test_migrates_legacy_db_without_description_column(self, tmp_path):
        db_path = str(tmp_path / "toggl.db")
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE time_entries (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, "
                "start_iso TEXT NOT NULL, duration_secs INTEGER NOT NULL)"
            )
            conn.execute("INSERT INTO time_entries VALUES (1, 1, '2026-01-01T10:00:00Z', 3600)")

        archive = TogglArchive(db_path)

        assert archive.has_missing_descriptions()
        assert archive.get_entries(1) == [{"start": "2026-01-01T10:00:00Z", "duration": 3600}]

    def test_upsert_stores_description_and_fills_legacy_rows(self, tmp_path):
        archive = TogglArchive(str(tmp_path / "toggl.db"))
        archive.upsert(
            [
                {
                    "id": 1,
                    "project_id": 1,
                    "description": "Yoga",
                    "start": "2026-01-01T10:00:00Z",
                    "stop": "x",
                    "duration": 3600,
                },
                {
                    "id": 2,
                    "project_id": 1,
                    "description": "Cycling",
                    "start": "2026-01-02T10:00:00Z",
                    "stop": "x",
                    "duration": 1800,
                },
                {
                    "id": 3,
                    "project_id": 2,
                    "description": "Yoga",
                    "start": "2026-01-03T10:00:00Z",
                    "stop": "x",
                    "duration": 4200,
                },
            ]
        )

        assert archive.get_description_entries("Yoga") == [
            {"start": "2026-01-01T10:00:00Z", "duration": 3600},
            {"start": "2026-01-03T10:00:00Z", "duration": 4200},
        ]
        assert not archive.has_missing_descriptions()

    def test_entries_without_description_are_not_missing(self, tmp_path):
        archive = TogglArchive(str(tmp_path / "toggl.db"))
        archive.upsert([{"id": 1, "project_id": 1, "start": "2026-01-01T10:00:00Z", "stop": "x", "duration": 60}])
        assert not archive.has_missing_descriptions()

    def test_clear_missing_descriptions(self, tmp_path):
        archive = TogglArchive(str(tmp_path / "toggl.db"))
        archive.upsert([{"id": 1, "project_id": 1, "start": "2026-01-01T10:00:00Z", "stop": "x", "duration": 60}])
        with sqlite3.connect(archive.db_path) as conn:
            conn.execute("UPDATE time_entries SET description = NULL")

        assert archive.clear_missing_descriptions() == 1
        assert not archive.has_missing_descriptions()


class TestSessionStats:
    def test_empty(self):
        assert session_stats([]) == {"count": 0}

    def test_ignores_zero_length_entries(self):
        assert session_stats([{"duration": 0}]) == {"count": 0}

    def test_stats(self):
        entries = [{"duration": 3600}, {"duration": 1800}, {"duration": 5400}, {"duration": 0}]
        assert session_stats(entries) == {
            "count": 3,
            "total_secs": 10800,
            "avg_secs": 3600,
            "median_secs": 3600,
            "min_secs": 1800,
            "max_secs": 5400,
        }


class TestMatchDescriptions:
    TITLES = ["🧘 Morning yoga + stretch", "🧘 Morning yoga", "Spanish"]

    def test_ignores_emoji_punctuation_and_case(self):
        assert match_descriptions("MORNING yoga stretch", self.TITLES) == ["🧘 Morning yoga + stretch"]

    def test_exact_title_wins_over_titles_containing_it(self):
        assert match_descriptions("morning yoga", self.TITLES) == ["🧘 Morning yoga"]

    def test_part_of_a_title(self):
        assert match_descriptions("yoga", self.TITLES) == ["🧘 Morning yoga", "🧘 Morning yoga + stretch"]
        assert match_descriptions("stretch", self.TITLES) == ["🧘 Morning yoga + stretch"]

    def test_no_match(self):
        assert match_descriptions("running", self.TITLES) == []


class TestVisitStats:
    def test_sums_same_day_entries_into_one_visit(self):
        entries = [
            {"start": "2026-06-01T18:00:00+02:00", "duration": 3000},
            {"start": "2026-06-01T19:00:00+02:00", "duration": 600},
            {"start": "2026-06-03T18:00:00+02:00", "duration": 3300},
        ]
        stats = visit_stats(entries)
        assert stats["count"] == 2
        assert stats["max_secs"] == 3600
        assert stats["min_secs"] == 3300

    def test_groups_by_local_day_not_utc(self):
        # 00:30 local on the 2nd is still the 1st in UTC
        entries = [
            {"start": "2026-06-01T22:00:00+02:00", "duration": 600},
            {"start": "2026-06-02T00:30:00+02:00", "duration": 600},
        ]
        assert visit_stats(entries)["count"] == 2

    def test_ignores_zero_length_entries(self):
        assert visit_stats([{"start": "2026-06-01T10:00:00Z", "duration": 0}]) == {"count": 0}


class TestFormatDuration:
    def test_under_an_hour(self):
        assert format_duration(45 * 60) == "45m"

    def test_hours_and_minutes(self):
        assert format_duration(3600 + 5 * 60) == "1h 05m"

    def test_rounds_to_nearest_minute(self):
        assert format_duration(3599) == "1h 00m"


class TestLoadIgnoredDescriptions:
    def test_missing(self, monkeypatch):
        monkeypatch.delenv("TOGGL_IGNORED_DESCRIPTIONS", raising=False)
        assert load_ignored_descriptions() == set()

    def test_valid_list(self, monkeypatch):
        monkeypatch.setenv("TOGGL_IGNORED_DESCRIPTIONS", '["Chores", "Inbox"]')
        assert load_ignored_descriptions() == {"Chores", "Inbox"}

    def test_invalid(self, monkeypatch):
        monkeypatch.setenv("TOGGL_IGNORED_DESCRIPTIONS", "Chores")
        assert load_ignored_descriptions() == set()


def _described(description: str, days: list[str]) -> list[dict]:
    return [{"description": description, "start": f"{d}T10:00:00Z", "duration": 600} for d in days]


class TestRecurringDescriptions:
    _TODAY = date(2026, 6, 28)  # 8-week window: 3 May – 28 Jun

    def test_needs_half_the_weeks(self, monkeypatch):
        monkeypatch.delenv("TOGGL_IGNORED_DESCRIPTIONS", raising=False)
        entries = (
            _described("Weekly", ["2026-05-05", "2026-05-12", "2026-05-19", "2026-05-26"])
            # many entries, but only in 3 distinct weeks
            + _described("Bursty", ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-09", "2026-06-16"])
            + _described("Once", ["2026-06-20"])
        )
        assert recurring_descriptions(entries, self._TODAY) == ["Weekly"]

    def test_ordered_by_recent_frequency(self, monkeypatch):
        monkeypatch.delenv("TOGGL_IGNORED_DESCRIPTIONS", raising=False)
        weeks = ["2026-05-05", "2026-05-12", "2026-05-19", "2026-05-26"]
        entries = _described("Less", weeks) + _described("More", weeks + ["2026-05-06", "2026-05-13"])
        assert recurring_descriptions(entries, self._TODAY) == ["More", "Less"]

    def test_report_window_keeps_recently_lapsed_routines(self, monkeypatch):
        monkeypatch.delenv("TOGGL_IGNORED_DESCRIPTIONS", raising=False)
        # Weekly from January to mid-April, then stopped: out of the 8-week window
        lapsed = _described("Lapsed", [str(date(2026, 1, 6) + timedelta(weeks=w)) for w in range(15)])
        assert recurring_descriptions(lapsed, self._TODAY) == []
        assert recurring_descriptions(lapsed, self._TODAY, REPORT_ROUTINES) == ["Lapsed"]

    def test_skips_empty_and_ignored(self, monkeypatch):
        monkeypatch.setenv("TOGGL_IGNORED_DESCRIPTIONS", '["Chores"]')
        weeks = ["2026-05-05", "2026-05-12", "2026-05-19", "2026-05-26"]
        entries = _described("", weeks) + _described("Chores", weeks) + _described("Yoga", weeks)
        assert recurring_descriptions(entries, self._TODAY) == ["Yoga"]


class TestSparkline:
    def test_scales_to_peak(self):
        assert sparkline([0, 1, 2, 4, 8]) == "▁▂▃▅█"

    def test_all_zero(self):
        assert sparkline([0, 0, 0]) == "▁▁▁"

    def test_small_nonzero_is_distinguishable_from_zero(self):
        assert sparkline([0, 0.01, 10]) == "▁▂█"


class TestGetDescribedEntries:
    def test_filters_empty_zero_and_old(self, tmp_path):
        archive = TogglArchive(str(tmp_path / "toggl.db"))
        archive.upsert(
            [
                {
                    "id": 1,
                    "project_id": 1,
                    "description": "Yoga",
                    "start": "2026-06-01T10:00:00Z",
                    "stop": "x",
                    "duration": 60,
                },
                {
                    "id": 2,
                    "project_id": 1,
                    "description": "",
                    "start": "2026-06-02T10:00:00Z",
                    "stop": "x",
                    "duration": 60,
                },
                {
                    "id": 3,
                    "project_id": 1,
                    "description": "Yoga",
                    "start": "2026-04-01T10:00:00Z",
                    "stop": "x",
                    "duration": 60,
                },
                {
                    "id": 4,
                    "project_id": 1,
                    "description": "Yoga",
                    "start": "2026-06-03T10:00:00Z",
                    "stop": "x",
                    "duration": 0,
                },
            ]
        )
        assert archive.get_described_entries(since="2026-05-01") == [
            {"id": 1, "description": "Yoga", "start": "2026-06-01T10:00:00Z", "duration": 60}
        ]
