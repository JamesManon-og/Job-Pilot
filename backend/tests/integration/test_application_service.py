"""Test ApplicationService: prepare, duplicate rejection, daily cap, submit flow."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from jobpilot.applications import ApplicationService, DailyCapReachedError
from jobpilot.config.preferences import UserPreferences
from jobpilot.database import create_session_factory
from jobpilot.database.repositories import (
    ApplicationRepository,
    DuplicateApplicationError,
    JobRepository,
    ResumeRepository,
)
from jobpilot.domain import ApplicationStatus, Job, JobSource, Resume


class FakeLLM:
    async def generate(self, prompt: str, *, system: str | None = None) -> str:
        return "1. Great company culture.\n2. My skills match perfectly."


@dataclass
class FakeAutofillResult:
    screenshot_path: str | None = "/tmp/screenshot.png"


class FakeAutofill:
    async def fill(
        self,
        url: str,
        *,
        cover_letter: str = "",
        extra_answers: dict[str, str] | None = None,
    ) -> FakeAutofillResult:
        return FakeAutofillResult()


class FailingAutofill:
    async def fill(self, url: str, **kwargs: object) -> object:
        raise RuntimeError("Browser crashed")


async def seed(engine: AsyncEngine) -> tuple[int, int]:
    factory = create_session_factory(engine)
    async with factory() as session:
        job, _ = await JobRepository(session).upsert(
            Job(
                title="Engineer",
                company="Acme",
                application_url="https://a.com/j/1",
                source=JobSource.REMOTEOK,
            )
        )
        resume = await ResumeRepository(session).add(
            Resume(version="v1", file_path="/r.pdf", is_active=True)
        )
        await session.commit()
    assert job.id is not None and resume.id is not None
    return job.id, resume.id


class TestApplicationService:
    async def test_prepare_creates_pending_application(self, engine: AsyncEngine) -> None:
        job_id, resume_id = await seed(engine)
        factory = create_session_factory(engine)
        service = ApplicationService(factory, UserPreferences(), llm=FakeLLM())

        app = await service.prepare(job_id, resume_id=resume_id)
        assert app.status is ApplicationStatus.PENDING_REVIEW
        assert app.cover_letter != ""
        assert "Why do you want to work here?" in app.answers

    async def test_prepare_rejects_duplicate(self, engine: AsyncEngine) -> None:
        job_id, resume_id = await seed(engine)
        factory = create_session_factory(engine)
        service = ApplicationService(factory, UserPreferences(), llm=FakeLLM())

        await service.prepare(job_id, resume_id=resume_id)
        with pytest.raises(DuplicateApplicationError):
            await service.prepare(job_id, resume_id=resume_id)

    async def test_daily_cap_enforced(self, engine: AsyncEngine) -> None:
        job_id, resume_id = await seed(engine)
        factory = create_session_factory(engine)
        prefs = UserPreferences(max_applications_per_day=1)
        service = ApplicationService(factory, prefs, llm=FakeLLM())

        app = await service.prepare(job_id, resume_id=resume_id)
        assert app.id is not None

        # Mark as submitted to use up the daily cap
        async with factory() as session:
            repo = ApplicationRepository(session)
            await repo.update_status(app.id, ApplicationStatus.SUBMITTED)
            await session.commit()

        # Add a second job
        async with factory() as session:
            await JobRepository(session).upsert(
                Job(
                    title="Designer",
                    company="Beta",
                    application_url="https://b.com/j/2",
                    source=JobSource.REMOTEOK,
                )
            )
            await session.commit()

        with pytest.raises(DailyCapReachedError):
            await service.prepare(2, resume_id=resume_id)

    async def test_submit_approved_application(self, engine: AsyncEngine) -> None:
        job_id, resume_id = await seed(engine)
        factory = create_session_factory(engine)
        service = ApplicationService(
            factory, UserPreferences(), llm=FakeLLM(), autofill=FakeAutofill()
        )

        app = await service.prepare(job_id, resume_id=resume_id)
        assert app.id is not None

        # Approve it first
        async with factory() as session:
            await ApplicationRepository(session).update_status(
                app.id, ApplicationStatus.APPROVED
            )
            await session.commit()

        submitted = await service.submit(app.id)
        assert submitted.status is ApplicationStatus.SUBMITTED
        assert submitted.submitted_at is not None

    async def test_submit_wrong_status_rejected(self, engine: AsyncEngine) -> None:
        job_id, resume_id = await seed(engine)
        factory = create_session_factory(engine)
        service = ApplicationService(factory, UserPreferences(), llm=FakeLLM())

        app = await service.prepare(job_id, resume_id=resume_id)
        assert app.id is not None

        with pytest.raises(ValueError, match="pending_review"):
            await service.submit(app.id)

    async def test_autofill_failure_marks_failed(self, engine: AsyncEngine) -> None:
        job_id, resume_id = await seed(engine)
        factory = create_session_factory(engine)
        service = ApplicationService(
            factory, UserPreferences(), llm=FakeLLM(), autofill=FailingAutofill()
        )

        app = await service.prepare(job_id, resume_id=resume_id)
        assert app.id is not None
        async with factory() as session:
            await ApplicationRepository(session).update_status(
                app.id, ApplicationStatus.APPROVED
            )
            await session.commit()

        result = await service.submit(app.id)
        assert result.status is ApplicationStatus.FAILED
        assert "Browser crashed" in result.notes

    async def test_prepare_without_llm_gives_empty_materials(self, engine: AsyncEngine) -> None:
        job_id, resume_id = await seed(engine)
        factory = create_session_factory(engine)
        service = ApplicationService(factory, UserPreferences())

        app = await service.prepare(job_id, resume_id=resume_id)
        assert app.cover_letter == ""
        assert app.answers == {}

    async def test_prepare_nonexistent_job_raises(self, engine: AsyncEngine) -> None:
        _, resume_id = await seed(engine)
        factory = create_session_factory(engine)
        service = ApplicationService(factory, UserPreferences())

        with pytest.raises(ValueError, match="not found"):
            await service.prepare(999, resume_id=resume_id)
