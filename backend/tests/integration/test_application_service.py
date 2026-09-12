"""ApplicationService: prepare, duplicate rejection, daily cap, and the truthful submit flow."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from jobpilot.applications import (
    ApplicationService,
    BlacklistedCompanyError,
    DailyCapReachedError,
    IncompleteProfileError,
    InvalidTransitionError,
    local_day_start,
)
from jobpilot.applications.service import WHY_COMPANY_QUESTION, WHY_QUALIFIED_QUESTION
from jobpilot.config.preferences import ApplicantProfile, UserPreferences
from jobpilot.database import create_session_factory
from jobpilot.database.repositories import (
    DuplicateApplicationError,
    JobRepository,
    ResumeRepository,
)
from jobpilot.domain import ApplicationStatus, Job, JobSource, Resume, ResumeProfile
from jobpilot.llm import OllamaError

APPLICANT = ApplicantProfile(name="James Manon", email="james@example.com")


class FakeLLM:
    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        self.prompts: list[str] = []
        self._answers = answers or {
            "why_company": "Great company culture.",
            "why_qualified": "I have 2.5 years of React and shipped v2.0 of our app.",
        }

    async def generate(self, prompt: str, *, system: str | None = None) -> str:
        self.prompts.append(prompt)
        return "Dear team, ... James Manon"

    async def generate_json(
        self, prompt: str, *, system: str | None = None, schema: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.prompts.append(prompt)
        return self._answers


class BrokenLLM:
    async def generate(self, prompt: str, *, system: str | None = None) -> str:
        raise OllamaError("model crashed")

    async def generate_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        raise OllamaError("model crashed")


async def seed(engine: AsyncEngine, *, company: str = "Acme", n: int = 1) -> tuple[int, int]:
    factory = create_session_factory(engine)
    async with factory() as session:
        job, _ = await JobRepository(session).upsert(
            Job(
                title=f"Engineer {n}",
                company=company,
                application_url=f"https://a.com/j/{n}",
                source=JobSource.REMOTEOK,
            )
        )
        resume = await ResumeRepository(session).get_by_version("v1")
        if resume is None:
            resume = await ResumeRepository(session).add(
                Resume(
                    version="v1",
                    file_path="/r.pdf",
                    is_active=True,
                    profile=ResumeProfile(technologies=["react", "fastapi"], years_experience=3),
                )
            )
        await session.commit()
    assert job.id is not None and resume.id is not None
    return job.id, resume.id


def prefs(**kwargs: Any) -> UserPreferences:
    return UserPreferences(applicant=APPLICANT, **kwargs)


class TestPrepare:
    async def test_prepare_creates_pending_application(self, engine: AsyncEngine) -> None:
        job_id, resume_id = await seed(engine)
        service = ApplicationService(create_session_factory(engine), prefs(), llm=FakeLLM())

        app = await service.prepare(job_id, resume_id=resume_id)
        assert app.status is ApplicationStatus.PENDING_REVIEW
        assert app.cover_letter != ""
        assert WHY_COMPANY_QUESTION in app.answers

    async def test_materials_are_grounded_in_the_resume(self, engine: AsyncEngine) -> None:
        """Bug: the cover-letter prompt contained only the job, so the model
        invented the candidate's experience and wrote "[Your Name]"."""
        job_id, resume_id = await seed(engine)
        llm = FakeLLM()
        service = ApplicationService(create_session_factory(engine), prefs(), llm=llm)
        await service.prepare(job_id, resume_id=resume_id)

        cover_prompt = llm.prompts[0]
        assert "react" in cover_prompt and "fastapi" in cover_prompt
        assert "James Manon" in cover_prompt
        assert "Years of experience: 3" in cover_prompt

    async def test_answers_containing_version_numbers_are_not_mangled(
        self, engine: AsyncEngine
    ) -> None:
        """Bug: answers were split on the first "2." — "2.5 years" broke them apart."""
        job_id, resume_id = await seed(engine)
        service = ApplicationService(create_session_factory(engine), prefs(), llm=FakeLLM())
        app = await service.prepare(job_id, resume_id=resume_id)
        assert app.answers[WHY_QUALIFIED_QUESTION] == (
            "I have 2.5 years of React and shipped v2.0 of our app."
        )
        assert app.answers[WHY_COMPANY_QUESTION] == "Great company culture."

    async def test_llm_failure_still_prepares_with_a_note(self, engine: AsyncEngine) -> None:
        """Bug: an Ollama error during cover-letter generation crashed the pipeline."""
        job_id, resume_id = await seed(engine)
        service = ApplicationService(create_session_factory(engine), prefs(), llm=BrokenLLM())
        app = await service.prepare(job_id, resume_id=resume_id)
        assert app.status is ApplicationStatus.PENDING_REVIEW
        assert app.cover_letter == ""
        assert "Cover letter generation failed" in app.notes

    async def test_prepare_rejects_duplicate(self, engine: AsyncEngine) -> None:
        job_id, resume_id = await seed(engine)
        service = ApplicationService(create_session_factory(engine), prefs(), llm=FakeLLM())
        await service.prepare(job_id, resume_id=resume_id)
        with pytest.raises(DuplicateApplicationError):
            await service.prepare(job_id, resume_id=resume_id)

    async def test_blacklisted_company_is_never_prepared(self, engine: AsyncEngine) -> None:
        """Bug: jobs scraped before a company was blacklisted could still be prepared."""
        job_id, resume_id = await seed(engine, company="SpamCorp")
        service = ApplicationService(
            create_session_factory(engine), prefs(blacklist_companies=["spamcorp"])
        )
        with pytest.raises(BlacklistedCompanyError):
            await service.prepare(job_id, resume_id=resume_id)

    async def test_prepare_without_llm_gives_empty_materials(self, engine: AsyncEngine) -> None:
        job_id, resume_id = await seed(engine)
        service = ApplicationService(create_session_factory(engine), prefs())
        app = await service.prepare(job_id, resume_id=resume_id)
        assert app.cover_letter == ""
        assert app.answers == {}

    async def test_prepare_nonexistent_job_raises(self, engine: AsyncEngine) -> None:
        _, resume_id = await seed(engine)
        service = ApplicationService(create_session_factory(engine), prefs())
        with pytest.raises(ValueError, match="not found"):
            await service.prepare(999, resume_id=resume_id)


class TestApprovalAndSubmission:
    async def _approved(self, engine: AsyncEngine, service: ApplicationService, n: int) -> int:
        job_id, resume_id = await seed(engine, n=n)
        app = await service.prepare(job_id, resume_id=resume_id)
        assert app.id is not None
        await service.approve(app.id)
        return app.id

    async def test_submission_requires_approval_then_claim_then_confirmation(
        self, engine: AsyncEngine
    ) -> None:
        """Bug: submit() marked applications SUBMITTED after a headless autofill
        that never clicked submit and closed the browser — nothing was sent."""
        service = ApplicationService(create_session_factory(engine), prefs())
        job_id, resume_id = await seed(engine)
        app = await service.prepare(job_id, resume_id=resume_id)
        assert app.id is not None

        with pytest.raises(InvalidTransitionError):
            await service.mark_submitted(app.id)  # not approved
        with pytest.raises(InvalidTransitionError):
            await service.claim_for_autofill(app.id)  # not approved

        await service.approve(app.id)
        with pytest.raises(InvalidTransitionError):
            await service.mark_submitted(app.id)  # approved but never opened

        claimed, job = await service.claim_for_autofill(app.id)
        assert claimed.status is ApplicationStatus.AWAITING_CONFIRMATION
        assert job.id == job_id
        submitted = await service.mark_submitted(app.id)
        assert submitted.status is ApplicationStatus.SUBMITTED
        assert submitted.submitted_at is not None
        with pytest.raises(InvalidTransitionError):
            await service.mark_submitted(app.id)  # never twice

    async def test_approving_twice_is_refused(self, engine: AsyncEngine) -> None:
        service = ApplicationService(create_session_factory(engine), prefs())
        app_id = await self._approved(engine, service, 1)
        with pytest.raises(InvalidTransitionError):
            await service.approve(app_id)

    async def test_released_application_goes_back_to_approved(self, engine: AsyncEngine) -> None:
        service = ApplicationService(create_session_factory(engine), prefs())
        app_id = await self._approved(engine, service, 1)
        await service.claim_for_autofill(app_id)
        released = await service.release(app_id, notes="later")
        assert released.status is ApplicationStatus.APPROVED

    async def test_daily_cap_blocks_opening_more_forms(self, engine: AsyncEngine) -> None:
        service = ApplicationService(
            create_session_factory(engine), prefs(max_applications_per_day=1)
        )
        first = await self._approved(engine, service, 1)
        second = await self._approved(engine, service, 2)
        await service.claim_for_autofill(first)
        with pytest.raises(DailyCapReachedError):
            await service.claim_for_autofill(second)

    async def test_daily_cap_blocks_prepare_once_reached(self, engine: AsyncEngine) -> None:
        service = ApplicationService(
            create_session_factory(engine), prefs(max_applications_per_day=1)
        )
        first = await self._approved(engine, service, 1)
        await service.claim_for_autofill(first)
        await service.mark_submitted(first)
        job_id, resume_id = await seed(engine, n=2)
        with pytest.raises(DailyCapReachedError):
            await service.prepare(job_id, resume_id=resume_id)

    async def test_claim_refused_without_applicant_name_and_email(
        self, engine: AsyncEngine
    ) -> None:
        """No submitting if required information is missing."""
        service = ApplicationService(create_session_factory(engine), prefs())
        app_id = await self._approved(engine, service, 1)
        incomplete = ApplicationService(create_session_factory(engine), UserPreferences())
        with pytest.raises(IncompleteProfileError, match="applicant.name"):
            await incomplete.claim_for_autofill(app_id)


class TestLocalDay:
    def test_day_starts_at_local_midnight_not_utc(self) -> None:
        """Bug: the cap reset at UTC midnight — 08:00 for a user in Manila."""
        manila = timezone(timedelta(hours=8))
        now = datetime(2026, 9, 12, 7, 30, tzinfo=manila)  # 23:30 UTC the previous day
        start = local_day_start(now)
        assert start.astimezone(manila) == datetime(2026, 9, 12, 0, 0, tzinfo=manila)
        assert start.astimezone(UTC) == datetime(2026, 9, 11, 16, 0, tzinfo=UTC)
