# Production-ish API image.
#
# Differences from docker/api.Dockerfile (the development image), and why:
#   * installs from requirements.lock, so a rebuild cannot silently pick up a new
#     upstream release;
#   * installs the project without the dev extra, so pytest/ruff/httptools are not
#     shipped to production;
#   * runs as a non-root user;
#   * has a healthcheck that consults real readiness rather than process liveness.
FROM python:3.12-slim AS base

WORKDIR /app

COPY requirements.lock pyproject.toml README.md ./
COPY apps/api/src ./apps/api/src
COPY scripts/healthcheck.py ./scripts/healthcheck.py

# Resolve from the lock file first (the layer that changes least), then install
# the project itself with --no-deps so it cannot pull a different version than
# the lock file pinned.
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.lock \
    && pip install --no-cache-dir -e . --no-deps

# Drop privileges. A container that can write to its own code is a container that
# can be turned into a persistent foothold.
RUN useradd --create-home --shell /usr/sbin/nologin traver \
    && chown -R traver:traver /app
USER traver

ENV PYTHONPATH=/app/apps/api/src \
    PYTHONUNBUFFERED=1 \
    # Fail loudly instead of writing .pyc into a read-only layer.
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

# Readiness, not liveness: the endpoint round-trips the database, so an instance
# that cannot reach its storage is reported unhealthy rather than merely "up".
# The probe lives in scripts/healthcheck.py so the JSON handling is readable and
# testable instead of a quoted one-liner.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "scripts/healthcheck.py"]

# No --reload here: reload is a development affordance that watches the
# filesystem and must never be enabled in production.
CMD ["python", "-m", "travel_api"]
