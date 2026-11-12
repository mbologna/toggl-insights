FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

# Install uv
COPY requirements-uv.txt /tmp/
RUN pip install --no-cache-dir --root-user-action=ignore --require-hashes -r /tmp/requirements-uv.txt

WORKDIR /app

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install the locked runtime dependencies with their hashes; --locked fails
# the build if uv.lock no longer matches pyproject.toml
RUN uv export --quiet --locked --no-dev --no-emit-project --no-header -o /tmp/requirements.txt \
    && uv pip install --system --no-cache --require-hashes -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# pip only installs the image's dependencies at build time. Removing it
# (with the packages it vendors) and ensurepip's bundled pip wheel keeps
# their CVEs out of the image: nothing runs pip at run time.
RUN /usr/local/bin/python3 -m pip uninstall -y --root-user-action=ignore pip \
    && rm -rf /usr/local/lib/python3*/ensurepip

# Copy application code
COPY src/ ./src/

# Run as non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER 1000:1000

# Unbuffer Python output
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080"]
