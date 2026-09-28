# Development API image.
#
# Priorities here are a fast edit/run loop, not a small or hardened artifact:
#   * the dev extra is installed (pytest, ruff) so tests can run in the container;
#   * source is bind-mounted by docker-compose, so this copy is only a fallback;
#   * it runs as root, which is acceptable locally and is NOT what
#     docker/api.prod.Dockerfile does.
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY apps/api/src ./apps/api/src
# The compose healthcheck executes this, so it must exist in the image; the
# source bind-mount alone would hide that dependency until someone ran the image
# without compose.
COPY scripts/healthcheck.py ./scripts/healthcheck.py

RUN pip install --no-cache-dir .[dev]

ENV PYTHONPATH=/app/apps/api/src
EXPOSE 8000
