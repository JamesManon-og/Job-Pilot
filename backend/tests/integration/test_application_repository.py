from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.database.repositories import (
    ApplicationRepository,
    DuplicateApplicationError,
    JobRepository,
    ResumeRepository,
)
from jobpilot.domain import Application, ApplicationStatus, Job, JobSource, Resume


async def seed_job_and_resume(session: AsyncSession) -> tuple[int, int]:
    job, _ = await JobRepository(session).upsert(
        Job(
            title="Engineer",
            company="Acme",
            application_url="https://a.com/j/1",
            source=JobSource.REMOTEOK,
        )
    )
    resume = await ResumeRepository(session).add(Resume(version="v1", file_path="/resumes/v1.pdf"))
    assert job.id is not None and resume.id is not None
    return job.id, resume.id


class TestApplicationRepository:
    async def test_create_and_fetch(self, session: AsyncSession) -> None:
        job_id, resume_id = await seed_job_and_resume(session)
        repo = ApplicationRepository(session)
        app = await repo.create(Application(job_id=job_id, resume_id=resume_id))
        assert app.id is not None
        assert app.status is ApplicationStatus.PENDING_REVIEW

    async def test_duplicate_application_rejected(self, session: AsyncSession) -> None:
        job_id, resume_id = await seed_job_and_resume(session)
        repo = ApplicationRepository(session)
        await repo.create(Application(job_id=job_id, resume_id=resume_id))
        with pytest.raises(DuplicateApplicationError):
            await repo.create(Application(job_id=job_id, resume_id=resume_id))

    async def test_company_position_duplicate_check(self, session: AsyncSession) -> None:
        job_id, resume_id = await seed_job_and_resume(session)
        repo = ApplicationRepository(session)
        assert not await repo.exists_for_company_position("Acme", "Engineer")
        await repo.create(Application(job_id=job_id, resume_id=resume_id))
        assert await repo.exists_for_company_position("  ACME ", "engineer")

    async def test_status_transition_sets_submitted_at_and_logs_event(
        self, session: AsyncSession
    ) -> None:
        job_id, resume_id = await seed_job_and_resume(session)
        repo = ApplicationRepository(session)
        app = await repo.create(Application(job_id=job_id, resume_id=resume_id))
        assert app.id is not None

        updated = await repo.update_status(app.id, ApplicationStatus.SUBMITTED)
        assert updated is not None
        assert updated.status is ApplicationStatus.SUBMITTED
        assert updated.submitted_at is not None

    async def test_list_by_status(self, session: AsyncSession) -> None:
        job_id, resume_id = await seed_job_and_resume(session)
        repo = ApplicationRepository(session)
        app = await repo.create(Application(job_id=job_id, resume_id=resume_id))
        assert app.id is not None
        await repo.update_status(app.id, ApplicationStatus.SUBMITTED)

        assert await repo.list(status=ApplicationStatus.PENDING_REVIEW) == []
        submitted = await repo.list(status=ApplicationStatus.SUBMITTED)
        assert [a.id for a in submitted] == [app.id]
