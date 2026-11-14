# AGENTS.md — toggl-insights

Toggl time tracking dashboard and trend alerts. User-facing docs: `README.md`.

## Layout

```
src/toggl_insights/main.py     # FastAPI dashboard + background sync; console script toggl-insights (the image's entry point)
src/toggl_insights/alerts.py   # scheduled Apprise reports; console script toggl-alerts --digest|--monthly|--session-gap
src/toggl_insights/cleanup.py  # spurious-entry cleanup; console script toggl-cleanup
src/toggl_insights/toggl.py    # Reports API client and the SQLite archive
src/toggl_insights/templates/  # the dashboard page (package data)
compat/src/                    # transitional `python -m src.<module>` shims for older deployments; remove when unused
tests/                         # pytest; keep I/O out of the logic so it tests without mocks
scripts/demo.py                # the dashboard on generated data, no Toggl account needed
Dockerfile                     # two stages: uv sync --locked into /opt/venv, then a pip-less runtime
```

## Conventions

- Conventional Commits, header at most 72 characters. The squash-merge
  subject is the release note: `feat`/`fix`/`perf` release, `refactor`,
  `test`, `docs`, `ci`, `chore` don't (a dependency update to runtime files
  ships in the weekly maintenance release).
- Releases are tags; `pyproject.toml`'s version stays `0.0.0`.
- Coverage may not drop below `fail_under` in `pyproject.toml`; raise it
  when tests are added.
- Dependencies: `uv add <pkg>`; uv.lock is committed and the image installs
  it with `--locked`. `exclude-newer` keeps releases younger than 7 days out.
- Notifications go through `apprise_notify.notify()` with the `APPRISE_URL`
  environment variable: one URL or several space-separated ones, tried in
  order. It never raises; check its return value when sending matters.
- The image: non-root (uid 1000), no pip, nothing written outside `/tmp`
  or a mounted volume.

## Template

The skeleton comes from https://github.com/mbologna/python-app-template
(`.copier-answers.yml`). Renovate's copier manager opens a pull request
for each template release; resolve conflicts in favour of this project's
code. By hand: `uvx copier update --trust`.
