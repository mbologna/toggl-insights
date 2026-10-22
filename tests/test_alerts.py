"""Unit tests for alerts.py pure-function helpers."""

import json
import os
import subprocess
import sys
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.alerts import (
    _alert_on_empty_projects,
    _change,
    _get_client_and_archive,
    _hours_for_month,
    _hours_for_week,
    _iso_week_start,
    _last_completed_iso_week,
    _rate,
    _rolling_monthly_avg,
    _rolling_weekly_avg,
    _send_alert,
    _shift_month,
    _week_label,
    run_monthly_report,
    run_session_gap_alerts,
    run_weekly_digest,
)
from src.toggl import TogglArchive, TogglReportsClient


def _entry(start: str, duration_s: int) -> dict:
    return {"start": start, "duration": duration_s}


def _recorder(sent: list[tuple[str, list[str]]]):
    """A _send_alert stand-in that records (title, lines) and reports success."""

    def fake_send(url: str, title: str, lines: list[str]) -> bool:
        sent.append((title, lines))
        return True

    return fake_send


# ---------------------------------------------------------------------------
# _send_alert
# ---------------------------------------------------------------------------


class TestSendAlert:
    _URL = "http://apprise.local/notify/x"

    def test_dry_run_short_circuits(self, monkeypatch):
        monkeypatch.setenv("ALERT_DRY_RUN", "true")
        with patch("src.alerts.requests.Session.post") as mock_post:
            assert _send_alert(self._URL, "t", ["a", "b"]) is True
            mock_post.assert_not_called()

    def test_success_uses_generous_connect_timeout(self, monkeypatch):
        monkeypatch.delenv("ALERT_DRY_RUN", raising=False)
        with patch("src.alerts.requests.Session.post") as mock_post:
            mock_post.return_value = MagicMock(raise_for_status=lambda: None)
            assert _send_alert(self._URL, "t", ["line"]) is True
            _, kwargs = mock_post.call_args
            connect_timeout, _read_timeout = kwargs["timeout"]
            # Must tolerate a cold cross-cluster WireGuard connect (was 3.05s).
            assert connect_timeout >= 10

    def test_retries_are_configured_for_post(self, monkeypatch):
        """The session must opt POST into urllib3 retries (off by default)."""
        monkeypatch.delenv("ALERT_DRY_RUN", raising=False)
        from src.alerts import _build_apprise_session

        session = _build_apprise_session()
        retry = session.get_adapter("http://apprise.local").max_retries
        assert retry.connect and retry.connect >= 1
        assert "POST" in retry.allowed_methods

    def test_connection_error_returns_false(self, monkeypatch):
        monkeypatch.delenv("ALERT_DRY_RUN", raising=False)
        with patch("src.alerts.requests.Session.post", side_effect=requests.exceptions.ConnectTimeout("cold")):
            assert _send_alert(self._URL, "t", ["line"]) is False


# ---------------------------------------------------------------------------
# _get_client_and_archive
# ---------------------------------------------------------------------------


class TestGetClientAndArchive:
    """Regression test for the real TogglReportsClient(api_token, workspace_id)
    signature — deliberately does NOT mock the constructor itself (unlike the
    run_* tests below). This is the test that would have caught
    `TogglReportsClient(api_token)` being called with a missing `workspace_id`
    argument.
    """

    def test_builds_real_client_and_archive_without_hitting_the_network(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TOGGL_API_TOKEN", "fake-token")
        monkeypatch.setenv("TOGGL_WORKSPACE_ID", "123456")
        monkeypatch.setenv("TOGGL_DB_PATH", str(tmp_path / "toggl.db"))

        with patch("src.toggl.requests.post") as mock_post:
            client, archive = _get_client_and_archive()

        assert isinstance(client, TogglReportsClient)
        assert isinstance(archive, TogglArchive)
        assert client.workspace_id == 123456
        # The whole point: reads the already-synced archive, no Reports API call.
        mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# _alert_on_empty_projects
# ---------------------------------------------------------------------------


class TestAlertOnEmptyProjects:
    """get_projects() returning nothing used to just log a warning and
    silently return from every run_* function — no notification anywhere.
    This covers the meta-alert that now fires instead.
    """

    def test_sends_alert_and_sets_cooldown(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ALERT_DRY_RUN", "true")
        archive = TogglArchive(str(tmp_path / "toggl.db"))

        _alert_on_empty_projects(archive, "http://apprise/notify")

        assert archive.is_on_cooldown("no_projects", 0, cooldown_days=5)

    def test_skips_when_on_cooldown(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ALERT_DRY_RUN", "true")
        archive = TogglArchive(str(tmp_path / "toggl.db"))
        archive.mark_alerted("no_projects", [0])

        with patch("src.alerts._send_alert") as mock_send:
            _alert_on_empty_projects(archive, "http://apprise/notify")

        mock_send.assert_not_called()

    def test_sends_actual_payload(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ALERT_DRY_RUN", raising=False)
        archive = TogglArchive(str(tmp_path / "toggl.db"))

        with patch("src.alerts.requests.Session.post") as mock_post:
            mock_post.return_value = MagicMock(raise_for_status=lambda: None)
            _alert_on_empty_projects(archive, "http://apprise/notify")

        _, kwargs = mock_post.call_args
        assert kwargs["json"]["title"] == "Toggl Insights: alerting is down"


# ---------------------------------------------------------------------------
# _iso_week_start
# ---------------------------------------------------------------------------


class TestIsoWeekStart:
    def test_week1_2024(self):
        # ISO week 1 of 2024 starts on Monday 2024-01-01
        assert _iso_week_start(2024, 1) == date(2024, 1, 1)

    def test_week1_2026(self):
        # ISO week 1 of 2026 starts on Monday 2025-12-29
        assert _iso_week_start(2026, 1) == date(2025, 12, 29)

    def test_week_26_2025(self):
        d = _iso_week_start(2025, 26)
        assert d.weekday() == 0  # Monday
        assert d.isocalendar()[1] == 26


# ---------------------------------------------------------------------------
# _last_completed_iso_week
# ---------------------------------------------------------------------------


class TestLastCompletedIsoWeek:
    def test_monday_returns_previous_week(self):
        # On Monday 2026-06-29 (start of week 27), last completed = week 26
        today = date(2026, 6, 29)
        year, week = _last_completed_iso_week(today)
        assert year == 2026
        assert week == 26

    def test_friday_returns_previous_week(self):
        # On Friday 2026-07-03 (week 27), last completed = week 26
        today = date(2026, 7, 3)
        year, week = _last_completed_iso_week(today)
        assert year == 2026
        assert week == 26

    def test_week_boundary_jan(self):
        # On Mon 2025-01-06 (week 2 of 2025), last completed = week 1
        today = date(2025, 1, 6)
        year, week = _last_completed_iso_week(today)
        assert year == 2025
        assert week == 1


# ---------------------------------------------------------------------------
# _hours_for_week
# ---------------------------------------------------------------------------


class TestHoursForWeek:
    ENTRIES = [
        _entry("2026-06-22T09:00:00Z", 3600),  # Monday W26
        _entry("2026-06-23T10:00:00Z", 7200),  # Tuesday W26
        _entry("2026-06-29T09:00:00Z", 1800),  # Monday W27 (excluded)
        _entry("2026-06-21T08:00:00Z", 3600),  # Sunday W25 (excluded)
    ]

    def test_sums_week26(self):
        total = _hours_for_week(self.ENTRIES, 2026, 26)
        assert abs(total - 3.0) < 0.01  # 1h + 2h

    def test_excludes_other_weeks(self):
        assert _hours_for_week(self.ENTRIES, 2026, 27) == pytest.approx(0.5)

    def test_empty_entries(self):
        assert _hours_for_week([], 2026, 26) == 0.0

    def test_negative_duration_ignored(self):
        entries = [_entry("2026-06-22T09:00:00Z", -1)]  # running timer
        assert _hours_for_week(entries, 2026, 26) == 0.0

    def test_z_suffix_handled(self):
        entries = [_entry("2026-06-22T09:00:00Z", 3600)]
        assert _hours_for_week(entries, 2026, 26) == pytest.approx(1.0)

    def test_offset_suffix_handled(self):
        entries = [_entry("2026-06-22T11:00:00+02:00", 3600)]
        assert _hours_for_week(entries, 2026, 26) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# _rolling_weekly_avg
# ---------------------------------------------------------------------------


class TestRollingWeeklyAvg:
    def test_uniform_entries(self):
        # 10h each week across 6 weeks; avg of last 4 completed = 10h
        entries = []
        # Weeks 22-27 of 2026; today is Monday W27 → last completed = W26
        for week in range(22, 28):
            monday = _iso_week_start(2026, week)
            entries.append(_entry(f"{monday}T09:00:00Z", 36000))  # 10h
        today = date(2026, 6, 29)  # Monday W27
        avg = _rolling_weekly_avg(entries, today, weeks=4)
        assert abs(avg - 10.0) < 0.1

    def test_no_entries(self):
        avg = _rolling_weekly_avg([], date(2026, 6, 29), weeks=4)
        assert avg == 0.0


# ---------------------------------------------------------------------------
# _hours_for_month
# ---------------------------------------------------------------------------


class TestHoursForMonth:
    ENTRIES = [
        _entry("2026-06-01T09:00:00Z", 3600),  # June
        _entry("2026-06-30T09:00:00Z", 7200),  # June
        _entry("2026-07-01T09:00:00Z", 1800),  # July (excluded)
        _entry("2026-05-31T09:00:00Z", 3600),  # May (excluded)
    ]

    def test_sums_june(self):
        total = _hours_for_month(self.ENTRIES, 2026, 6)
        assert abs(total - 3.0) < 0.01

    def test_excludes_adjacent_months(self):
        assert _hours_for_month(self.ENTRIES, 2026, 7) == pytest.approx(0.5)

    def test_december_boundary(self):
        entries = [_entry("2026-12-31T09:00:00Z", 3600)]
        assert _hours_for_month(entries, 2026, 12) == pytest.approx(1.0)

    def test_empty(self):
        assert _hours_for_month([], 2026, 6) == 0.0


# ---------------------------------------------------------------------------
# _rolling_monthly_avg
# ---------------------------------------------------------------------------


class TestRollingMonthlyAvg:
    def test_uniform_months(self):
        # 20h each month: Jun, May, Apr = 20h avg
        entries = []
        for month, _day_count in [(4, 30), (5, 31), (6, 30)]:
            entries.append(_entry(f"2026-{month:02d}-15T09:00:00Z", 72000))  # 20h
        today = date(2026, 7, 15)
        avg = _rolling_monthly_avg(entries, today, months=3)
        assert abs(avg - 20.0) < 0.1

    def test_year_boundary(self):
        # Today in Jan 2026 → looks back at Dec 2025, Nov 2025, Oct 2025
        entries = [
            _entry("2025-12-15T09:00:00Z", 36000),
            _entry("2025-11-15T09:00:00Z", 36000),
            _entry("2025-10-15T09:00:00Z", 36000),
        ]
        today = date(2026, 1, 15)
        avg = _rolling_monthly_avg(entries, today, months=3)
        assert abs(avg - 10.0) < 0.1

    def test_empty(self):
        assert _rolling_monthly_avg([], date(2026, 7, 15)) == 0.0


# ---------------------------------------------------------------------------
# _entry_local_date (timezone boundary)
# ---------------------------------------------------------------------------


class TestEntryLocalDate:
    # _ALERT_TZ is read from ALERT_TIMEZONE at import time, so patch it directly.
    @pytest.fixture(autouse=True)
    def _berlin(self, monkeypatch):
        from zoneinfo import ZoneInfo

        monkeypatch.setattr("src.alerts._ALERT_TZ", ZoneInfo("Europe/Berlin"))

    def test_utc_z_suffix(self):
        # 23:30 UTC on June 22 = 01:30 June 23 in Europe/Berlin (CEST UTC+2)
        from src.alerts import _entry_local_date

        d = _entry_local_date("2026-06-22T23:30:00Z")
        assert d.isoformat() == "2026-06-23"

    def test_early_morning_utc(self):
        # 08:00 UTC = 10:00 Berlin — same date
        from src.alerts import _entry_local_date

        d = _entry_local_date("2026-06-22T08:00:00Z")
        assert d.isoformat() == "2026-06-22"


# ---------------------------------------------------------------------------
# run_weekly_digest
# ---------------------------------------------------------------------------


class TestRunWeeklyDigest:
    """Tests for run_weekly_digest() using mocked external dependencies."""

    # W26 of 2026: Mon 2026-06-22 – Sun 2026-06-28
    # Today = Monday 2026-06-29 (W27) → last completed = W26
    _TODAY = date(2026, 6, 29)

    def _make_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TOGGL_API_TOKEN", "fake-token")
        monkeypatch.setenv("TOGGL_WORKSPACE_ID", "123456")
        monkeypatch.setenv("APPRISE_URL", "http://apprise/notify")
        monkeypatch.setenv("TOGGL_DB_PATH", ":memory:")
        monkeypatch.setenv("ALERT_DRY_RUN", "true")

    def _make_mocks(
        self,
        projects: list[dict],
        entries_by_id: dict[int, list[dict]],
        today: date = _TODAY,
    ):
        mock_client = MagicMock()
        mock_client.get_projects.return_value = projects
        mock_client.fetch_raw.return_value = []

        mock_archive = MagicMock()
        mock_archive.upsert.return_value = 0
        mock_archive.get_entries.side_effect = lambda pid: entries_by_id.get(pid, [])

        return mock_client, mock_archive

    def test_sends_correct_lines_when_entries_exist(self, monkeypatch):
        self._make_env(monkeypatch)
        projects = [
            {"id": 1, "name": "Reading"},
            {"id": 2, "name": "Fitness"},
        ]
        entries = {
            1: [{"start": "2026-06-22T09:00:00Z", "duration": 45000}],  # 12.5h W26
            2: [{"start": "2026-06-23T10:00:00Z", "duration": 7200}],  # 2.0h W26
        }
        mock_client, mock_archive = self._make_mocks(projects, entries)

        sent_calls: list[tuple] = []

        def fake_send(url: str, title: str, lines: list[str]) -> bool:
            sent_calls.append((title, lines))
            return True

        with (
            patch("src.alerts.TogglReportsClient", return_value=mock_client),
            patch("src.alerts.TogglArchive", return_value=mock_archive),
            patch("src.alerts.date") as mock_date,
            patch("src.alerts._send_alert", side_effect=fake_send),
        ):
            mock_date.today.return_value = self._TODAY
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            run_weekly_digest()

        assert len(sent_calls) == 1
        title, lines = sent_calls[0]
        assert title == "Week 22–28 Jun"
        assert any("Reading" in ln and "12.5" in ln for ln in lines)
        assert any("Fitness" in ln and "2.0" in ln for ln in lines)

    def test_skips_send_when_no_entries(self, monkeypatch):
        self._make_env(monkeypatch)
        projects = [{"id": 1, "name": "Reading"}]
        entries: dict[int, list[dict]] = {1: []}
        mock_client, mock_archive = self._make_mocks(projects, entries)

        sent_calls: list = []

        with (
            patch("src.alerts.TogglReportsClient", return_value=mock_client),
            patch("src.alerts.TogglArchive", return_value=mock_archive),
            patch("src.alerts.date") as mock_date,
            patch("src.alerts._send_alert", side_effect=lambda *a, **kw: sent_calls.append(a)),
        ):
            mock_date.today.return_value = self._TODAY
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            run_weekly_digest()

        assert len(sent_calls) == 0

    def test_alerts_when_no_projects_found(self, monkeypatch):
        """Wiring check: the real run_weekly_digest() calls through to
        _alert_on_empty_projects() when get_projects() returns nothing,
        instead of silently returning like it used to."""
        self._make_env(monkeypatch)
        mock_client, mock_archive = self._make_mocks(projects=[], entries_by_id={})

        with (
            patch("src.alerts.TogglReportsClient", return_value=mock_client),
            patch("src.alerts.TogglArchive", return_value=mock_archive),
            patch("src.alerts.date") as mock_date,
            patch("src.alerts._alert_on_empty_projects") as mock_meta_alert,
        ):
            mock_date.today.return_value = self._TODAY
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            run_weekly_digest()

        mock_meta_alert.assert_called_once_with(mock_archive, "http://apprise/notify")

    def test_lines_sorted_descending_by_hours(self, monkeypatch):
        self._make_env(monkeypatch)
        projects = [
            {"id": 1, "name": "Alpha"},
            {"id": 2, "name": "Beta"},
            {"id": 3, "name": "Gamma"},
        ]
        entries = {
            1: [{"start": "2026-06-22T09:00:00Z", "duration": 3600}],  # 1.0h
            2: [{"start": "2026-06-22T09:00:00Z", "duration": 36000}],  # 10.0h
            3: [{"start": "2026-06-22T09:00:00Z", "duration": 18000}],  # 5.0h
        }
        mock_client, mock_archive = self._make_mocks(projects, entries)

        captured_lines: list[str] = []

        def fake_send(url: str, title: str, lines: list[str]) -> bool:
            captured_lines.extend(lines)
            return True

        with (
            patch("src.alerts.TogglReportsClient", return_value=mock_client),
            patch("src.alerts.TogglArchive", return_value=mock_archive),
            patch("src.alerts.date") as mock_date,
            patch("src.alerts._send_alert", side_effect=fake_send),
        ):
            mock_date.today.return_value = self._TODAY
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            run_weekly_digest()

        # Project lines (exclude the total line at the end)
        project_lines = [ln for ln in captured_lines if ln.startswith("•")]
        names_in_order = [ln.split(":")[0].lstrip("• ") for ln in project_lines]
        assert names_in_order == ["Beta", "Gamma", "Alpha"]

    def test_total_line_present(self, monkeypatch):
        self._make_env(monkeypatch)
        projects = [
            {"id": 1, "name": "Reading"},
            {"id": 2, "name": "Fitness"},
        ]
        entries = {
            1: [{"start": "2026-06-22T09:00:00Z", "duration": 7200}],  # 2.0h
            2: [{"start": "2026-06-22T09:00:00Z", "duration": 10800}],  # 3.0h
        }
        mock_client, mock_archive = self._make_mocks(projects, entries)

        captured_lines: list[str] = []

        def fake_send(url: str, title: str, lines: list[str]) -> bool:
            captured_lines.extend(lines)
            return True

        with (
            patch("src.alerts.TogglReportsClient", return_value=mock_client),
            patch("src.alerts.TogglArchive", return_value=mock_archive),
            patch("src.alerts.date") as mock_date,
            patch("src.alerts._send_alert", side_effect=fake_send),
        ):
            mock_date.today.return_value = self._TODAY
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            run_weekly_digest()

        total_lines = [ln for ln in captured_lines if ln.startswith("Total:")]
        assert len(total_lines) == 1
        assert "5.0" in total_lines[0]


# ---------------------------------------------------------------------------
# Shared harness for the run_* entrypoints
# ---------------------------------------------------------------------------


def _d(description: str, start: str, duration_s: int) -> dict:
    """A described entry, as TogglArchive.get_described_entries returns them."""
    return {"description": description, "start": start, "duration": duration_s}


def _run_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
    fn,
    today: date,
    projects: list[dict] | None = None,
    entries_by_id: dict[int, list[dict]] | None = None,
    described: list[dict] | None = None,
    last_entry: str | None = None,
    on_cooldown: bool = False,
    targets: dict[str, float] | None = None,
) -> tuple[list[tuple[str, list[str]]], MagicMock]:
    """Run an entrypoint against mocked Toggl/archive/Apprise; return (sent, archive)."""
    monkeypatch.setenv("TOGGL_API_TOKEN", "fake-token")
    monkeypatch.setenv("TOGGL_WORKSPACE_ID", "123456")
    monkeypatch.setenv("APPRISE_URL", "http://apprise/notify")
    monkeypatch.delenv("TREND_ALERT_THRESHOLDS", raising=False)
    monkeypatch.setenv("TOGGL_WEEKLY_TARGETS", json.dumps(targets or {}))

    mock_client = MagicMock()
    mock_client.get_projects.return_value = projects or []
    mock_archive = MagicMock()
    mock_archive.get_entries.side_effect = lambda pid: (entries_by_id or {}).get(pid, [])
    mock_archive.get_described_entries.side_effect = lambda since: [e for e in (described or []) if e["start"] >= since]
    mock_archive.get_last_entry_time.return_value = last_entry
    mock_archive.is_on_cooldown.return_value = on_cooldown

    sent: list[tuple[str, list[str]]] = []
    with (
        patch("src.alerts.TogglReportsClient", return_value=mock_client),
        patch("src.alerts.TogglArchive", return_value=mock_archive),
        patch("src.alerts.date") as mock_date,
        patch("src.alerts._send_alert", side_effect=_recorder(sent)),
    ):
        mock_date.today.return_value = today
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        fn()
    return sent, mock_archive


class TestChange:
    def test_no_baseline(self):
        assert _change(0, 0, 0.25) == ""
        assert _change(3, 0, 0.25) == "new"

    def test_negligible_change_is_omitted(self):
        assert _change(10.4, 10, 0.25) == ""

    def test_within_threshold_has_no_arrow(self):
        assert _change(11, 10, 0.25) == "+10%"
        assert _change(8, 10, 0.25) == "-20%"

    def test_beyond_threshold_has_arrow(self):
        assert _change(15, 10, 0.25) == "+50% ↑"
        assert _change(5, 10, 0.25) == "-50% ↓"


class TestLabels:
    def test_week_label_same_month(self):
        assert _week_label(date(2026, 10, 5)) == "5–11 Oct"

    def test_week_label_across_months(self):
        assert _week_label(date(2026, 9, 28)) == "28 Sep – 4 Oct"

    def test_week_label_across_years(self):
        assert _week_label(date(2025, 12, 29)) == "29 Dec 2025 – 4 Jan 2026"

    def test_rate_drops_trailing_zero(self):
        assert _rate(2.8) == "2.8"
        assert _rate(18.0) == "18"


class TestShiftMonth:
    def test_back_across_year(self):
        assert _shift_month(2026, 1, -1) == (2025, 12)

    def test_forward_across_year(self):
        assert _shift_month(2026, 11, 2) == (2027, 1)

    def test_back_several(self):
        assert _shift_month(2026, 6, -5) == (2026, 1)


class TestWeeklyDigestFormat:
    # Today = Monday 2026-06-29 → reports W26 (22–28 Jun) vs W22–W25 (25 May – 21 Jun)
    _TODAY = date(2026, 6, 29)
    _PRIOR_WEEKS = ["2026-05-26", "2026-06-02", "2026-06-09", "2026-06-16"]

    def _weekly(self, last_week_secs: int, prior_secs: int) -> list[dict]:
        entries = [_entry(f"{d}T10:00:00Z", prior_secs) for d in self._PRIOR_WEEKS]
        return entries + [_entry("2026-06-23T10:00:00Z", last_week_secs)]

    def test_shows_change_vs_prior_four_weeks(self, monkeypatch):
        projects = [{"id": 1, "name": "Fitness"}, {"id": 2, "name": "Work"}]
        entries = {1: self._weekly(7200, 14400), 2: self._weekly(36000, 34200)}

        sent, _ = _run_entrypoint(monkeypatch, run_weekly_digest, self._TODAY, projects, entries)

        title, lines = sent[0]
        assert title == "Week 22–28 Jun"
        assert "• Fitness: 2.0 h  -50% ↓" in lines
        assert "• Work: 10.0 h  +5%" in lines
        assert "Total: 12.0 h  -11%" in lines
        assert lines[-1] == "% vs average of the previous 4 weeks"

    def test_target_markers(self, monkeypatch):
        projects = [{"id": 1, "name": "Met"}, {"id": 2, "name": "Missed"}, {"id": 3, "name": "Over"}]
        entries = {1: self._weekly(18000, 18000), 2: self._weekly(3600, 3600), 3: self._weekly(36000, 36000)}
        monkeypatch.setenv("OVER_TARGET_THRESHOLD", "0.5")

        sent, _ = _run_entrypoint(
            monkeypatch, run_weekly_digest, self._TODAY, projects, entries, targets={"1": 5, "2": 5, "3": 5}
        )

        _, lines = sent[0]
        assert "• Met: 5.0 h · target 5.0 h ✓" in lines
        assert "• Missed: 1.0 h · target 5.0 h" in lines
        assert "• Over: 10.0 h · target 5.0 h ⚠ +100%" in lines

    def test_omits_idle_projects(self, monkeypatch):
        projects = [{"id": 1, "name": "Active"}, {"id": 2, "name": "Idle"}]
        entries = {1: self._weekly(3600, 3600), 2: [_entry("2025-01-01T10:00:00Z", 3600)]}

        sent, _ = _run_entrypoint(monkeypatch, run_weekly_digest, self._TODAY, projects, entries)

        _, lines = sent[0]
        assert not any("Idle" in ln for ln in lines)

    def test_recurring_section(self, monkeypatch):
        projects = [{"id": 1, "name": "Fitness"}]
        entries = {1: self._weekly(3600, 3600)}
        yoga_prior = ["05-26", "05-28", "06-02", "06-04", "06-09", "06-11", "06-16", "06-18"]
        described = [_d("Yoga", f"2026-{d}T17:00:00Z", 3600) for d in yoga_prior]
        described += [
            _d("Yoga", "2026-06-22T17:00:00Z", 3300),  # 55m
            _d("Yoga", "2026-06-24T17:00:00Z", 6000),  # 1h 40m
            _d("Yoga", "2026-06-26T17:00:00Z", 4200),  # 1h 10m
        ]
        described += [_d("Spanish", f"2026-{d}T07:00:00Z", 1800) for d in ("05-27", "06-03", "06-10", "06-17")]
        described += [_d("Some documentary", "2026-06-23T20:00:00Z", 6000)]  # one-off: never recurring

        sent, _ = _run_entrypoint(monkeypatch, run_weekly_digest, self._TODAY, projects, entries, described)

        _, lines = sent[0]
        start = lines.index("Recurring:")
        assert lines[start + 1 : start + 3] == [
            "• Yoga: 3× · avg 1h 15m (usually 2×/wk · 1h 00m)",
            "• Spanish: none (usually 1×/wk)",
        ]
        assert not any("Some documentary" in ln for ln in lines)


class TestRunMonthlyReport:
    # Today = 2026-07-01 → reports June; sparklines cover Jan–Jun
    _TODAY = date(2026, 7, 1)

    def test_hours_and_recurring(self, monkeypatch):
        projects = [{"id": 1, "name": "Fitness"}]
        hours = [_entry(f"2026-{m:02d}-15T10:00:00Z", 18000) for m in range(1, 6)]  # 5h Jan–May
        hours.append(_entry("2026-06-15T10:00:00Z", 36000))  # 10h June
        yoga = [_d("Yoga", f"2026-{m:02d}-{d:02d}T10:00:00Z", 3600) for m in (1, 2, 3) for d in (5, 20)]
        yoga += [_d("Yoga", f"2026-{m:02d}-{d:02d}T10:00:00Z", 4500) for m in (4, 5, 6) for d in (3, 10, 17, 24)]

        sent, _ = _run_entrypoint(monkeypatch, run_monthly_report, self._TODAY, projects, {1: hours}, yoga)

        title, lines = sent[0]
        assert title == "June 2026"
        assert "• Fitness: 10.0 h  +100% ↑  ▅▅▅▅▅█" in lines
        assert "Total: 10.0 h  +100% ↑  ▅▅▅▅▅█" in lines
        assert "• Yoga: 4× (0.9/wk)  ▄▅▄███ · avg 1h 15m" in lines
        assert lines[-1] == "% vs average of the previous 3 months · bars = last 6 months"

    def test_skips_when_month_empty(self, monkeypatch):
        projects = [{"id": 1, "name": "Fitness"}]
        sent, _ = _run_entrypoint(monkeypatch, run_monthly_report, self._TODAY, projects, {1: []})
        assert sent == []

    def test_alerts_when_no_projects_found(self, monkeypatch):
        with patch("src.alerts._alert_on_empty_projects") as mock_meta_alert:
            _run_entrypoint(monkeypatch, run_monthly_report, self._TODAY, projects=[])
        mock_meta_alert.assert_called_once()


class TestRunSessionGapAlerts:
    _TODAY = date(2026, 6, 29)

    @staticmethod
    def _yoga_until_jun_22() -> list[dict]:
        """Mon/Wed/Fri from 11 May, last session Mon 22 Jun: 19 active days in 8 weeks
        → usual interval ~3 days → lapse after 6."""
        days = [date(2026, 5, 11) + timedelta(days=offset) for offset in range(0, 42) if offset % 7 in (0, 2, 4)]
        days.append(date(2026, 6, 22))
        return [_d("Yoga", f"{d}T17:00:00Z", 3600) for d in days]

    @staticmethod
    def _spanish_recent() -> list[dict]:
        return [_d("Spanish", f"2026-{d}T07:00:00Z", 1800) for d in ("06-06", "06-13", "06-20", "06-27")]

    def test_alerts_only_on_lapsed_routines(self, monkeypatch):
        described = self._yoga_until_jun_22() + self._spanish_recent()

        sent, archive = _run_entrypoint(
            monkeypatch, run_session_gap_alerts, self._TODAY, described=described, last_entry="2026-06-28T09:00:00Z"
        )

        title, lines = sent[0]
        assert title == "Lapsed routines"
        assert lines == ["Yoga: 7 days since last time (Mon 22 Jun), usually every 3 days"]
        archive.mark_alerted.assert_called_once_with("session_gap:Yoga", [0])

    def test_not_lapsed_within_usual_interval(self, monkeypatch):
        sent, _ = _run_entrypoint(
            monkeypatch,
            run_session_gap_alerts,
            date(2026, 6, 26),
            described=self._yoga_until_jun_22(),
            last_entry="2026-06-25T09:00:00Z",
        )
        assert sent == []

    def test_respects_cooldown(self, monkeypatch):
        sent, archive = _run_entrypoint(
            monkeypatch, run_session_gap_alerts, self._TODAY, described=self._yoga_until_jun_22(), on_cooldown=True
        )
        assert sent == []
        archive.is_on_cooldown.assert_called_once_with("session_gap:Yoga", 0, 5)

    def test_ignored_descriptions(self, monkeypatch):
        monkeypatch.setenv("TOGGL_IGNORED_DESCRIPTIONS", '["Yoga"]')
        sent, _ = _run_entrypoint(monkeypatch, run_session_gap_alerts, self._TODAY, described=self._yoga_until_jun_22())
        assert sent == []

    def test_nothing_tracked_collapses_to_sync_warning(self, monkeypatch):
        sent, _ = _run_entrypoint(
            monkeypatch,
            run_session_gap_alerts,
            self._TODAY,
            described=self._yoga_until_jun_22(),
            last_entry="2026-06-22T17:00:00Z",
        )
        assert sent[0][1] == [
            "⚠ Nothing tracked at all since 22 Jun (1 routine overdue) — is the dashboard sync stuck?"
        ]

    def test_no_recurring_activities(self, monkeypatch):
        sent, _ = _run_entrypoint(monkeypatch, run_session_gap_alerts, self._TODAY, described=[])
        assert sent == []


class TestEntrypointDispatch:
    @pytest.mark.parametrize("argv", [[], ["--midweek"], ["--digest", "--monthly"]])
    def test_rejects_missing_or_unknown_flags(self, argv):
        result = subprocess.run(
            [sys.executable, "-m", "src.alerts", *argv],
            capture_output=True,
            text=True,
            env={**os.environ, "APPRISE_URL": ""},
        )
        assert result.returncode != 0
        assert "usage: python -m src.alerts" in result.stderr


class TestLoadPerProjectThresholds:
    def test_valid_json(self, monkeypatch):
        from src.alerts import _load_per_project_thresholds

        monkeypatch.setenv("TREND_ALERT_THRESHOLDS", '{"123": 0.15, "456": 0.3}')
        result = _load_per_project_thresholds()
        assert result == {"123": 0.15, "456": 0.3}

    def test_empty_env(self, monkeypatch):
        from src.alerts import _load_per_project_thresholds

        monkeypatch.delenv("TREND_ALERT_THRESHOLDS", raising=False)
        assert _load_per_project_thresholds() == {}

    def test_invalid_json(self, monkeypatch):
        from src.alerts import _load_per_project_thresholds

        monkeypatch.setenv("TREND_ALERT_THRESHOLDS", "not-json")
        assert _load_per_project_thresholds() == {}
