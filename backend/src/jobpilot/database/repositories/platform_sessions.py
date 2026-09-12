"""Saved-login bookkeeping per platform (status only; never credentials)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.database.orm import PlatformSessionRow
from jobpilot.domain.enums import JobSource, SessionStatus
from jobpilot.domain.models import PlatformSession, utcnow


def _to_domain(row: PlatformSessionRow) -> PlatformSession:
    return PlatformSession(
        platform=JobSource(row.platform),
        status=SessionStatus(row.status),
        detail=row.detail,
        last_checked_at=row.last_checked_at,
        last_login_at=row.last_login_at,
    )


class PlatformSessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, platform: JobSource) -> PlatformSession | None:
        row = await self._session.get(PlatformSessionRow, platform.value)
        return _to_domain(row) if row else None

    async def list(self) -> list[PlatformSession]:
        rows = await self._session.scalars(select(PlatformSessionRow))
        return [_to_domain(row) for row in rows]

    async def record(
        self, platform: JobSource, status: SessionStatus, *, detail: str = ""
    ) -> PlatformSession:
        row = await self._session.get(PlatformSessionRow, platform.value)
        if row is None:
            row = PlatformSessionRow(platform=platform.value)
            self._session.add(row)
        now = utcnow()
        if status is SessionStatus.LOGGED_IN and row.status != SessionStatus.LOGGED_IN.value:
            row.last_login_at = now
        row.status = status.value
        row.detail = detail
        row.last_checked_at = now
        await self._session.flush()
        return _to_domain(row)
