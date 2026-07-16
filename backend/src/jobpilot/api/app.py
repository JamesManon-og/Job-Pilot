"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import jobpilot
from jobpilot.api.deps import AppState
from jobpilot.api.routers import actions, applications, jobs, matches, scrape_runs, stats


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    state = AppState()
    app.state.jobpilot = state
    try:
        yield
    finally:
        await state.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="JobPilot", version=jobpilot.__version__, lifespan=_lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": jobpilot.__version__}

    app.include_router(stats.router)
    app.include_router(jobs.router)
    app.include_router(matches.router)
    app.include_router(applications.router)
    app.include_router(scrape_runs.router)
    app.include_router(actions.router)
    return app
