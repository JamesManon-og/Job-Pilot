"""FastAPI dependency wiring: one engine per app, one session per request."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from jobpilot.config import Settings, UserPreferences, get_settings, load_preferences
from jobpilot.database import create_engine, create_session_factory


class AppState:
    """Long-lived resources shared across requests."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.engine: AsyncEngine = create_engine(self.settings.database_url)
        self.session_factory: async_sessionmaker[AsyncSession] = create_session_factory(self.engine)

    def preferences(self) -> UserPreferences:
        # Re-read on each call so config.yaml edits apply without restart.
        return load_preferences(self.settings.preferences_path)

    async def dispose(self) -> None:
        await self.engine.dispose()


def get_state(request: Request) -> AppState:
    state: AppState = request.app.state.jobpilot
    return state


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    state = get_state(request)
    async with state.session_factory() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
StateDep = Annotated[AppState, Depends(get_state)]
