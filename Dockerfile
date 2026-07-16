FROM python:3.12-slim AS base

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
        libcups2 libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 \
        libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
        libcairo2 libasound2 && \
    rm -rf /var/lib/apt/lists/*

COPY backend/pyproject.toml backend/
COPY backend/src/ backend/src/
COPY backend/migrations/ backend/migrations/
COPY backend/alembic.ini backend/

RUN pip install --no-cache-dir ./backend

RUN playwright install --with-deps chromium

COPY config/ config/

VOLUME ["/app/data", "/app/config"]

ENV JOBPILOT_DATA_DIR=/app/data \
    JOBPILOT_PREFERENCES_PATH=/app/config/config.yaml

EXPOSE 8000

CMD ["python", "-m", "jobpilot", "serve", "--host", "0.0.0.0", "--port", "8000"]
