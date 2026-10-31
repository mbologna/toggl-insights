FROM python:3.15-slim@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913

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

# Copy application code
COPY src/ ./src/

# Run as non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER 1000:1000

# Unbuffer Python output
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080"]
