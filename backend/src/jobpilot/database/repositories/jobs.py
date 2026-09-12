"""Job persistence.

Identity lookup chain (first hit wins): (source, external_id) → dedup_hash →
canonical URL → raw URL. So a posting is one row across re-scrapes, tracking
parameters, title edits, and legacy rows from before external ids existed.
A new row whose fingerprint (company + title) matches an existing job is the
same job listed elsewhere: it's kept but marked SKIPPED, so it's never scored
or applied to twice.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.database.orm import JobRow
from jobpilot.domain.enums import (
    EmploymentType,
    ExperienceLevel,
    JobSource,
    JobStatus,
    RemoteType,
)
from jobpilot.domain.identity import identity_hash
from jobpilot.domain.models import Job, utcnow


def _to_domain(row: JobRow) -> Job:
    return Job(
        id=row.id,
        title=row.title,
        company=row.company,
        location=row.location,
        salary_raw=row.salary_raw,
        salary_min=row.salary_min,
        salary_max=row.salary_max,
        employment_type=EmploymentType(row.employment_type),
        experience_level=ExperienceLevel(row.experience_level),
        remote=RemoteType(row.remote),
        visa_sponsorship=row.visa_sponsorship,
        description=row.description,
        requirements=row.requirements,
        benefits=row.benefits,
        technologies=row.technologies,
        application_url=row.application_url,
        source=JobSource(row.source),
        date_posted=row.date_posted,
        scraped_at=row.scraped_at,
        external_id=row.external_id,
        canonical_url=row.canonical_url,
        dedup_hash=row.dedup_hash,
        fingerprint=row.fingerprint,
        status=JobStatus(row.status),
        status_reason=row.status_reason,
        duplicate_of_id=row.duplicate_of_id,
        last_seen_at=row.last_seen_at,
    )


def _apply_to_row(job: Job, row: JobRow) -> None:
    """Copy scraped content onto a row. Pipeline state is never overwritten here."""
    row.title = job.title
    row.company = job.company
    row.location = job.location
    row.salary_raw = job.salary_raw
    row.salary_min = job.salary_min
    row.salary_max = job.salary_max
    row.employment_type = job.employment_type.value
    row.experience_level = job.experience_level.value
    row.remote = job.remote.value
    row.visa_sponsorship = job.visa_sponsorship
    if job.description or not row.description:  # a listing-only refresh keeps details
        row.description = job.description
    row.requirements = job.requirements or row.requirements or []
    row.benefits = job.benefits or row.benefits or []
    row.technologies = job.technologies or row.technologies or []
    row.application_url = job.application_url
    row.source = job.source.value
    row.date_posted = job.date_posted or row.date_posted
    row.scraped_at = job.scraped_at
    row.external_id = job.external_id or row.external_id
    row.canonical_url = job.canonical_url
    row.dedup_hash = job.dedup_hash
    row.fingerprint = job.fingerprint
    row.last_seen_at = utcnow()


class JobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _find_row(self, job: Job) -> JobRow | None:
        row: JobRow | None
        if job.external_id:
            row = await self._session.scalar(
                select(JobRow).where(
                    JobRow.source == job.source.value, JobRow.external_id == job.external_id
                )
            )
            if row is not None:
                return row
        for condition in (
            JobRow.dedup_hash == job.dedup_hash,
            JobRow.canonical_url == job.canonical_url,
            JobRow.application_url == job.application_url,
        ):
            row = await self._session.scalar(
                select(JobRow).where(condition).order_by(JobRow.id).limit(1)
            )
            if row is not None and _compatible(row, job):
                return row
        return None

    async def find_existing(self, job: Job) -> Job | None:
        """The stored row for this posting, if we've seen it before."""
        row = await self._find_row(job)
        return _to_domain(row) if row else None

    async def upsert(self, job: Job) -> tuple[Job, bool]:
        """Insert the job, or refresh the existing row for the same posting.

        Returns (persisted job, created) where created is True for new rows.
        """
        existing = await self._find_row(job)
        if existing is not None:
            if existing.external_id and job.external_id is None:
                # Keep the id-based identity; this listing just didn't carry the id.
                job = job.model_copy(
                    update={
                        "external_id": existing.external_id,
                        "dedup_hash": identity_hash(
                            job.source.value, existing.external_id, job.canonical_url
                        ),
                    }
                )
            _apply_to_row(job, existing)
            await self._session.flush()
            return _to_domain(existing), False

        row = JobRow()
        _apply_to_row(job, row)
        row.status = JobStatus.DISCOVERED.value
        row.status_reason = ""
        original = await self._first_with_fingerprint(job.fingerprint)
        if original is not None:
            row.status = JobStatus.SKIPPED.value
            row.duplicate_of_id = original.id
            row.status_reason = (
                f"duplicate of #{original.id} ({original.source}: {original.title} @ "
                f"{original.company})"
            )
        self._session.add(row)
        await self._session.flush()
        return _to_domain(row), True

    async def _first_with_fingerprint(self, fingerprint: str) -> JobRow | None:
        row: JobRow | None = await self._session.scalar(
            select(JobRow)
            .where(JobRow.fingerprint == fingerprint, JobRow.duplicate_of_id.is_(None))
            .order_by(JobRow.id)
            .limit(1)
        )
        return row

    async def touch_seen(self, job_id: int) -> None:
        await self._session.execute(
            update(JobRow).where(JobRow.id == job_id).values(last_seen_at=utcnow())
        )

    async def set_status(
        self,
        job_id: int,
        status: JobStatus,
        *,
        reason: str = "",
        only_from: tuple[JobStatus, ...] | None = None,
    ) -> None:
        stmt = update(JobRow).where(JobRow.id == job_id)
        if only_from is not None:
            stmt = stmt.where(JobRow.status.in_([s.value for s in only_from]))
        await self._session.execute(stmt.values(status=status.value, status_reason=reason))

    async def get(self, job_id: int) -> Job | None:
        row = await self._session.get(JobRow, job_id, populate_existing=True)
        return _to_domain(row) if row else None

    async def get_by_dedup_hash(self, dedup_hash: str) -> Job | None:
        row = await self._session.scalar(select(JobRow).where(JobRow.dedup_hash == dedup_hash))
        return _to_domain(row) if row else None

    async def list(
        self,
        *,
        source: JobSource | None = None,
        remote: RemoteType | None = None,
        status: JobStatus | None = None,
        search: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Job]:
        stmt = select(JobRow).order_by(JobRow.scraped_at.desc()).limit(limit).offset(offset)
        if source is not None:
            stmt = stmt.where(JobRow.source == source.value)
        if remote is not None:
            stmt = stmt.where(JobRow.remote == remote.value)
        if status is not None:
            stmt = stmt.where(JobRow.status == status.value)
        if search:
            pattern = f"%{search}%"
            stmt = stmt.where(
                or_(
                    JobRow.title.ilike(pattern),
                    JobRow.company.ilike(pattern),
                    JobRow.description.ilike(pattern),
                )
            )
        rows = await self._session.scalars(stmt)
        return [_to_domain(row) for row in rows]

    async def count(self) -> int:
        result = await self._session.scalar(select(func.count()).select_from(JobRow))
        return result or 0

    async def count_by_status(self) -> dict[str, int]:
        rows = await self._session.execute(
            select(JobRow.status, func.count()).group_by(JobRow.status)
        )
        return {status: count for status, count in rows.all()}

    async def last_seen_before(self, cutoff: datetime) -> int:
        result = await self._session.scalar(
            select(func.count()).select_from(JobRow).where(JobRow.last_seen_at < cutoff)
        )
        return result or 0


def _compatible(row: JobRow, job: Job) -> bool:
    """A URL/hash match is only the same posting if external ids don't disagree."""
    if row.source != job.source.value:
        return row.canonical_url == job.canonical_url  # same page, other source label
    return not (row.external_id and job.external_id and row.external_id != job.external_id)
