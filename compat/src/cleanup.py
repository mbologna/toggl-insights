"""Transitional: keeps `python -m src.cleanup` working in the image.

Deployments written before the package moved to src/toggl_insights still run
the old module path. This runs the toggl-cleanup console script's module instead;
remove compat/ once nothing uses `python -m src.cleanup`.
"""

import runpy

runpy.run_module("toggl_insights.cleanup", run_name="__main__", alter_sys=True)
