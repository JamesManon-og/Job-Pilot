"""Job persistence. Upserts are keyed on dedup_hash so re-scrapes never duplicate."""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.database.orm import JobRow
from jobpilot.domain.enums import EmploymentType, ExperienceLevel, JobSource, RemoteType
from jobpilot.domain.models import Job


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
        dedup_hash=row.dedup_hash,
    )


def _apply_to_row(job: Job, row: JobRow) -> None:
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
    row.description = job.description
    row.requirements = job.requirements
    row.benefits = job.benefits
    row.technologies = job.technologies
    row.application_url = job.application_url
    row.source = job.source.value
    row.date_posted = job.date_posted
    row.scraped_at = job.scraped_at
    row.dedup_hash = job.dedup_hash


class JobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, job: Job) -> tuple[Job, bool]:
        """Insert the job, or refresh the existing row for the same posting.

        A posting is identified by dedup_hash, falling back to application_url:
        boards edit titles in place, which changes the hash but not the URL, and
        inserting that as a new row would violate the unique URL index.

        Returns (persisted job, created) where created is True for new rows.
        """
        existing = await self._session.scalar(
            select(JobRow).where(JobRow.dedup_hash == job.dedup_hash)
        )
        if existing is None:
            existing = await self._session.scalar(
                select(JobRow).where(JobRow.application_url == job.application_url)
            )
        if existing is not None:
            _apply_to_row(job, existing)
            await self._session.flush()
            return _to_domain(existing), False

        row = JobRow()
        _apply_to_row(job, row)
        self._session.add(row)
        await self._session.flush()
        return _to_domain(row), True

    async def get(self, job_id: int) -> Job | None:
        row = await self._session.get(JobRow, job_id)
        return _to_domain(row) if row else None

    async def get_by_dedup_hash(self, dedup_hash: str) -> Job | None:
        row = await self._session.scalar(select(JobRow).where(JobRow.dedup_hash == dedup_hash))
        return _to_domain(row) if row else None

    async def list(
        self,
        *,
        source: JobSource | None = None,
        remote: RemoteType | None = None,
        search: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Job]:
        stmt = select(JobRow).order_by(JobRow.scraped_at.desc()).limit(limit).offset(offset)
        if source is not None:
            stmt = stmt.where(JobRow.source == source.value)
        if remote is not None:
            stmt = stmt.where(JobRow.remote == remote.value)
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
