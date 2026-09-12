FROM python:3.12-slim AS base

WORKDIR /app

COPY backend/pyproject.toml backend/
COPY backend/src/ backend/src/
COPY backend/migrations/ backend/migrations/
COPY backend/alembic.ini backend/

RUN pip install --no-cache-dir ./backend

# --with-deps installs exactly the system libraries this Chromium build needs
# (a hand-maintained apt list breaks when Debian renames packages).
RUN playwright install --with-deps chromium

COPY config/ config/

VOLUME ["/app/data", "/app/config"]

# project root = /app, so migrations are found at /app/backend and the
# database/config live on the mounted volumes.
ENV JOBPILOT_PROJECT_ROOT=/app \
    JOBPILOT_DATA_DIR=/app/data \
    JOBPILOT_PREFERENCES_PATH=/app/config/config.yaml

EXPOSE 8000

# Create/upgrade the schema on every start (idempotent), then serve the API.
# The dashboard API only: `jobpilot apply` needs a visible browser, so run it
# on the host.
CMD ["sh", "-c", "python -m jobpilot db init && exec python -m jobpilot serve --host 0.0.0.0 --port 8000"]
