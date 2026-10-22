"""Run the dashboard on generated, fictional data: no Toggl account needed.

    uv run python scripts/demo.py            # http://127.0.0.1:8000

Seeds a throwaway SQLite archive with three years of made-up time entries
for invented projects, stubs out the Toggl client (no network calls), and
starts the app. Used for the README screenshot.
"""

import os
import random
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

PROJECTS = [
    {"id": 1, "name": "Learning", "color": "#06aaf5", "active": True},
    {"id": 2, "name": "Fitness", "color": "#e36a00", "active": True},
    {"id": 3, "name": "Side project", "color": "#2da608", "active": True},
    {"id": 4, "name": "Reading", "color": "#c9806b", "active": True},
    {"id": 5, "name": "Gardening", "color": "#9e5bd9", "active": True},
]

# (project id, description, sessions per week, typical minutes, start year)
ROUTINES = [
    (1, "Spanish", 3.0, 40, 2024),
    (1, "Online course", 1.5, 75, 2025),
    (2, "Yoga", 2.5, 50, 2024),
    (2, "Cycling", 1.2, 90, 2024),
    (3, "Weather app", 2.0, 100, 2025),
    (4, "Novel", 4.0, 35, 2024),
    (5, "Vegetable patch", 1.0, 80, 2024),
]
ONE_OFFS = [(3, "Release prep"), (4, "Magazine"), (5, "Pruning"), (1, "Workshop")]


def seed(db_path: str, days: int = 3 * 365) -> str:
    from src.toggl import TogglArchive

    rng = random.Random(42)  # nosec B311: fake data, not security
    now = datetime.now(UTC)
    today = now.date()
    first = today - timedelta(days=days)
    entries, next_id = [], 1
    for offset in range(days + 1):
        day = first + timedelta(days=offset)
        weekend = day.weekday() >= 5
        # Busier in winter, lighter in August.
        season = 1.15 if day.month in (1, 2, 11, 12) else 0.6 if day.month == 8 else 1.0
        for pid, desc, per_week, minutes, since in ROUTINES:
            if day.year < since:
                continue
            chance = per_week / 7 * season * (1.4 if weekend else 0.85)
            if pid == 5:  # gardening follows the season
                chance *= 1.8 if day.month in (4, 5, 6, 9) else 0.2 if day.month in (12, 1, 2) else 1.0
            if rng.random() < chance:
                hour = rng.choice([7, 8, 12, 18, 19, 20, 21]) if not weekend else rng.randint(9, 18)
                start = datetime(day.year, day.month, day.day, hour, rng.choice([0, 15, 30, 45]), tzinfo=UTC)
                secs = max(600, int(rng.gauss(minutes, minutes * 0.3) * 60))
                if start + timedelta(seconds=secs) > now:
                    continue
                entries.append(_entry(next_id, pid, desc, start, secs))
                next_id += 1
        if rng.random() < 0.08:
            pid, desc = rng.choice(ONE_OFFS)
            start = datetime(day.year, day.month, day.day, rng.randint(9, 20), 0, tzinfo=UTC)
            secs = rng.randint(30, 150) * 60
            if start + timedelta(seconds=secs) > now:
                continue
            entries.append(_entry(next_id, pid, desc, start, secs))
            next_id += 1
    TogglArchive(db_path).upsert(entries)
    return first.isoformat()


def _entry(entry_id: int, project_id: int, description: str, start: datetime, secs: int) -> dict:
    return {
        "id": entry_id,
        "project_id": project_id,
        "description": description,
        "start": start.isoformat(),
        "stop": (start + timedelta(seconds=secs)).isoformat(),
        "duration": secs,
    }


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    db_path = os.path.join(tempfile.mkdtemp(prefix="toggl-demo-"), "toggl.db")
    os.environ.update(
        TOGGL_API_TOKEN="demo",  # nosec B106: placeholder, the client is stubbed
        TOGGL_WORKSPACE_ID="1",
        TOGGL_DB_PATH=db_path,
        TOGGL_DAILY_TARGET_HOURS="1.5",
        TOGGL_WEEKLY_TARGETS='{"1": 3, "2": 3.5, "3": 4}',
        TOGGL_YEARLY_GOALS='{"1": 150, "2": 180, "4": 120}',
    )
    os.environ["TOGGL_HISTORY_START_DATE"] = seed(db_path)

    from src.toggl import TogglReportsClient

    TogglReportsClient.get_projects = lambda self: PROJECTS  # type: ignore[method-assign]
    TogglReportsClient.fetch_raw = lambda self, start_date, end_date: []  # type: ignore[method-assign]

    import uvicorn

    from src.main import app

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
