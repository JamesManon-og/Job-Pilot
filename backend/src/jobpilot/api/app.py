"""Minimal FastAPI app skeleton. Routers are added milestone by milestone."""

from __future__ import annotations

from fastapi import FastAPI

import jobpilot


def create_app() -> FastAPI:
    app = FastAPI(title="JobPilot", version=jobpilot.__version__)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": jobpilot.__version__}

    return app
