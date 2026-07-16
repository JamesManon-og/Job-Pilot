"""Resume version persistence."""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.database.orm import ResumeRow
from jobpilot.domain.models import Resume, ResumeProfile


def _to_domain(row: ResumeRow) -> Resume:
    return Resume(
        id=row.id,
        version=row.version,
        file_path=row.file_path,
        profile=ResumeProfile.model_validate(row.profile),
        is_active=row.is_active,
        created_at=row.created_at,
    )


class ResumeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, resume: Resume) -> Resume:
        row = ResumeRow(
            version=resume.version,
            file_path=resume.file_path,
            profile=resume.profile.model_dump(),
            is_active=resume.is_active,
            created_at=resume.created_at,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_domain(row)

    async def get(self, resume_id: int) -> Resume | None:
        row = await self._session.get(ResumeRow, resume_id)
        return _to_domain(row) if row else None

    async def get_by_version(self, version: str) -> Resume | None:
        row = await self._session.scalar(select(ResumeRow).where(ResumeRow.version == version))
        return _to_domain(row) if row else None

    async def get_active(self) -> Resume | None:
        row = await self._session.scalar(select(ResumeRow).where(ResumeRow.is_active.is_(True)))
        return _to_domain(row) if row else None

    async def set_active(self, resume_id: int) -> None:
        """Make one resume active; all others become inactive."""
        await self._session.execute(update(ResumeRow).values(is_active=False))
        await self._session.execute(
            update(ResumeRow).where(ResumeRow.id == resume_id).values(is_active=True)
        )
        await self._session.flush()

    async def list(self) -> list[Resume]:
        rows = await self._session.scalars(select(ResumeRow).order_by(ResumeRow.created_at.desc()))
        return [_to_domain(row) for row in rows]
