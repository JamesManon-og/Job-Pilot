"""Application persistence with duplicate prevention and an audit event log."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.database.orm import ApplicationEventRow, ApplicationRow, JobRow
from jobpilot.domain.enums import ApplicationStatus
from jobpilot.domain.models import Application, utcnow


class DuplicateApplicationError(Exception):
    """Raised when an application already exists for the same job."""

    def __init__(self, job_id: int) -> None:
        super().__init__(f"An application already exists for job_id={job_id}")
        self.job_id = job_id


def _to_domain(row: ApplicationRow) -> Application:
    return Application(
        id=row.id,
        job_id=row.job_id,
        resume_id=row.resume_id,
        status=ApplicationStatus(row.status),
        cover_letter=row.cover_letter,
        answers=row.answers,
        screenshot_path=row.screenshot_path,
        external_application_id=row.external_application_id,
        notes=row.notes,
        submitted_at=row.submitted_at,
        created_at=row.created_at,
    )


class ApplicationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, application: Application) -> Application:
        """Persist a new application; refuses duplicates for the same job."""
        if await self.exists_for_job(application.job_id):
            raise DuplicateApplicationError(application.job_id)

        row = ApplicationRow(
            job_id=application.job_id,
            resume_id=application.resume_id,
            status=application.status.value,
            cover_letter=application.cover_letter,
            answers=application.answers,
            screenshot_path=application.screenshot_path,
            external_application_id=application.external_application_id,
            notes=application.notes,
            submitted_at=application.submitted_at,
            created_at=application.created_at,
        )
        self._session.add(row)
        await self._session.flush()
        await self.log_event(row.id, "created", {"status": row.status})
        return _to_domain(row)

    async def exists_for_job(self, job_id: int) -> bool:
        result = await self._session.scalar(
            select(func.count()).select_from(ApplicationRow).where(ApplicationRow.job_id == job_id)
        )
        return bool(result)

    async def exists_for_company_position(self, company: str, title: str) -> bool:
        """Cross-source duplicate check: same company + position via the jobs table."""
        result = await self._session.scalar(
            select(func.count())
            .select_from(ApplicationRow)
            .join(JobRow, JobRow.id == ApplicationRow.job_id)
            .where(
                func.lower(JobRow.company) == company.strip().lower(),
                func.lower(JobRow.title) == title.strip().lower(),
            )
        )
        return bool(result)

    async def get(self, application_id: int) -> Application | None:
        row = await self._session.get(ApplicationRow, application_id)
        return _to_domain(row) if row else None

    async def list(
        self, *, status: ApplicationStatus | None = None, limit: int = 100, offset: int = 0
    ) -> list[Application]:
        stmt = (
            select(ApplicationRow)
            .order_by(ApplicationRow.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if status is not None:
            stmt = stmt.where(ApplicationRow.status == status.value)
        rows = await self._session.scalars(stmt)
        return [_to_domain(row) for row in rows]

    async def update_status(
        self, application_id: int, status: ApplicationStatus, *, notes: str | None = None
    ) -> Application | None:
        row = await self._session.get(ApplicationRow, application_id)
        if row is None:
            return None
        previous = row.status
        row.status = status.value
        if status is ApplicationStatus.SUBMITTED and row.submitted_at is None:
            row.submitted_at = utcnow()
        if notes is not None:
            row.notes = notes
        await self._session.flush()
        await self.log_event(row.id, "status_changed", {"from": previous, "to": status.value})
        return _to_domain(row)

    async def set_screenshot(self, application_id: int, path: str) -> None:
        row = await self._session.get(ApplicationRow, application_id)
        if row is not None:
            row.screenshot_path = path
            await self._session.flush()

    async def count_submitted_since(self, since: datetime) -> int:
        """How many applications were submitted after `since` — powers the daily cap."""
        result = await self._session.scalar(
            select(func.count())
            .select_from(ApplicationRow)
            .where(ApplicationRow.submitted_at.is_not(None), ApplicationRow.submitted_at >= since)
        )
        return result or 0

    async def log_event(
        self, application_id: int, event_type: str, payload: dict[str, Any] | None = None
    ) -> None:
        self._session.add(
            ApplicationEventRow(
                application_id=application_id,
                event_type=event_type,
                payload=payload or {},
                created_at=utcnow(),
            )
        )
        await self._session.flush()
