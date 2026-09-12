"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse

import jobpilot
from jobpilot.api.deps import AppState
from jobpilot.api.routers import (
    actions,
    applications,
    jobs,
    matches,
    platforms,
    review,
    scrape_runs,
    stats,
)
from jobpilot.config import PreferencesError

ALLOWED_ORIGINS = ("http://localhost:3000", "http://127.0.0.1:3000")
# Host header allowlist: blocks DNS-rebinding pages from reading the local API.
# "test" is the host httpx's ASGI test client sends.
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]", "test"]
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    state = AppState()
    app.state.jobpilot = state
    try:
        yield
    finally:
        await state.dispose()


async def _reject_cross_site_writes(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """CSRF guard for the unauthenticated localhost API.

    CORS only stops other sites from *reading* responses; a plain form POST
    from any page the user visits would still approve applications. Writes
    must come from an allowed origin and carry a JSON content type or the
    X-JobPilot-Client header, both of which force a CORS preflight.
    """
    if request.method in _SAFE_METHODS:
        return await call_next(request)
    origin = request.headers.get("origin")
    if origin is not None and origin not in ALLOWED_ORIGINS:
        return JSONResponse({"detail": "Cross-origin request blocked"}, status_code=403)
    if request.headers.get("sec-fetch-site") == "cross-site":
        return JSONResponse({"detail": "Cross-site request blocked"}, status_code=403)
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json" and "x-jobpilot-client" not in request.headers:
        return JSONResponse(
            {"detail": "State-changing requests need Content-Type: application/json"},
            status_code=403,
        )
    return await call_next(request)


async def _preferences_error_handler(request: Request, exc: Exception) -> Response:
    return JSONResponse({"detail": str(exc)}, status_code=500)


def create_app() -> FastAPI:
    app = FastAPI(title="JobPilot", version=jobpilot.__version__, lifespan=_lifespan)

    app.middleware("http")(_reject_cross_site_writes)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(ALLOWED_ORIGINS),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
    app.add_exception_handler(PreferencesError, _preferences_error_handler)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": jobpilot.__version__}

    app.include_router(stats.router)
    app.include_router(jobs.router)
    app.include_router(matches.router)
    app.include_router(applications.router)
    app.include_router(scrape_runs.router)
    app.include_router(actions.router)
    app.include_router(review.router)
    app.include_router(platforms.router)
    return app
