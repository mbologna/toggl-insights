# Build stage: a pinned uv installs exactly what uv.lock pins into
# /opt/venv. Same base image and digest as the runtime stage, so compiled
# wheels match the Python they run on.
FROM python:3.15.2-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS build

# git: apprise-notify is a git dependency. Only this stage has it: the
# runtime stage gets the finished virtualenv. Not version-pinned: git never
# reaches the image, and the distribution drops old versions.
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
# uv from PyPI, hash-checked (requirements-uv.txt says why).
COPY requirements-uv.txt /tmp/
RUN pip install --no-cache-dir --root-user-action=ignore --require-hashes -r /tmp/requirements-uv.txt
# Install exactly what uv.lock pins, checking its hashes, rather than
# re-resolving pyproject.toml at build time; --locked fails the build when
# uv.lock is out of date. Bytecode is compiled here because the runtime image
# sets PYTHONDONTWRITEBYTECODE and runs read-only.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON=/usr/local/bin/python3 UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_CACHE=1
WORKDIR /tmp/build
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ src/
RUN uv sync --locked --no-dev --no-editable

FROM python:3.15.2-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
# The CI pipeline adds org.opencontainers.image.source and .revision, and
# .version from the release tag (pyproject.toml's version is a placeholder).
LABEL org.opencontainers.image.title="toggl-insights" \
      org.opencontainers.image.description="Toggl time tracking dashboard and trend alerts" \
      org.opencontainers.image.licenses="MIT"
COPY --from=build /opt/venv /opt/venv
# Run-time writes go to /tmp (HOME too), so the root filesystem can be
# read-only.
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
# The base image's pip (with the packages it vendors) and ensurepip's
# bundled pip wheel are never used at run time; removing them keeps their
# CVEs out of the image.
RUN /usr/local/bin/python3 -m pip uninstall -y --root-user-action=ignore pip \
    && rm -rf /usr/local/lib/python3*/ensurepip
# Transitional: deployments written before the src/ layout still run
# `python -m src.cleanup` and `python -m src.alerts`; compat/ runs the
# package's modules for them (python -m looks in the working directory).
# Remove both lines once they call toggl-cleanup and toggl-alerts.
WORKDIR /app
COPY compat/ ./
USER 1000:1000
ENTRYPOINT ["toggl-insights"]
