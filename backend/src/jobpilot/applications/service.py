"""Application orchestration: prepare, generate materials, submit.

Central coordination point that enforces every safety check:
- Duplicate prevention (same job_id, or same company+title across sources)
- Daily submission cap (max_applications_per_day)
- LLM-generated cover letter and answers
- Autofill via Playwright (never clicks submit unless auto-submit is on)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.config.preferences import UserPreferences
from jobpilot.database.repositories import (
    ApplicationRepository,
    DuplicateApplicationError,
    JobRepository,
)
from jobpilot.domain.enums import ApplicationStatus
from jobpilot.domain.models import Application, Job

logger = logging.getLogger(__name__)


class LLMProvider(Protocol):
    async def generate(self, prompt: str, *, system: str | None = None) -> str: ...


class AutofillProvider(Protocol):
    async def fill(
        self,
        url: str,
        *,
        cover_letter: str,
        extra_answers: dict[str, str] | None,
    ) -> object: ...


class DailyCapReachedError(Exception):
    def __init__(self, cap: int, submitted_today: int) -> None:
        super().__init__(f"Daily cap reached: {submitted_today}/{cap} applications submitted today")
        self.cap = cap
        self.submitted_today = submitted_today


class ApplicationService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        preferences: UserPreferences,
        llm: LLMProvider | None = None,
        autofill: AutofillProvider | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._prefs = preferences
        self._llm = llm
        self._autofill = autofill

    async def prepare(self, job_id: int, *, resume_id: int) -> Application:
        """Validate, generate materials, and create a PENDING_REVIEW application."""
        async with self._session_factory() as session:
            job_repo = JobRepository(session)
            app_repo = ApplicationRepository(session)

            job = await job_repo.get(job_id)
            if job is None:
                raise ValueError(f"Job {job_id} not found")

            if await app_repo.exists_for_job(job_id):
                raise DuplicateApplicationError(job_id)

            if await app_repo.exists_for_company_position(job.company, job.title):
                raise DuplicateApplicationError(job_id)

            await self._check_daily_cap(app_repo)

            cover_letter = await self._generate_cover_letter(job)
            answers = await self._generate_answers(job)

            application = await app_repo.create(
                Application(
                    job_id=job_id,
                    resume_id=resume_id,
                    status=ApplicationStatus.PENDING_REVIEW,
                    cover_letter=cover_letter,
                    answers=answers,
                )
            )
            await session.commit()
            logger.info(
                "Application %s prepared for %s at %s (pending review)",
                application.id,
                job.title,
                job.company,
            )
            return application

    async def submit(self, application_id: int) -> Application:
        """Submit an approved application via autofill."""
        async with self._session_factory() as session:
            app_repo = ApplicationRepository(session)
            job_repo = JobRepository(session)

            application = await app_repo.get(application_id)
            if application is None:
                raise ValueError(f"Application {application_id} not found")

            allowed = {ApplicationStatus.APPROVED}
            if self._prefs.auto_submit_enabled and not self._prefs.human_approval_enabled:
                allowed.add(ApplicationStatus.PENDING_REVIEW)

            if application.status not in allowed:
                raise ValueError(
                    f"Application {application_id} is {application.status.value}, "
                    f"expected one of: {', '.join(s.value for s in allowed)}"
                )

            await self._check_daily_cap(app_repo)

            job = await job_repo.get(application.job_id)
            if job is None:
                raise ValueError(f"Job {application.job_id} not found")

            if self._autofill is not None:
                try:
                    result = await self._autofill.fill(
                        job.application_url,
                        cover_letter=application.cover_letter,
                        extra_answers=application.answers or None,
                    )
                    screenshot_path = getattr(result, "screenshot_path", None)
                    if screenshot_path:
                        await app_repo.set_screenshot(application_id, screenshot_path)
                except Exception as exc:  # noqa: BLE001
                    logger.error("Autofill failed for application %s: %s", application_id, exc)
                    failed = await app_repo.update_status(
                        application_id, ApplicationStatus.FAILED, notes=str(exc)
                    )
                    await session.commit()
                    assert failed is not None
                    return failed

            updated = await app_repo.update_status(application_id, ApplicationStatus.SUBMITTED)
            await session.commit()
            assert updated is not None
            logger.info("Application %s submitted for %s", application_id, job.title)
            return updated

    async def prepare_and_submit(self, job_id: int, *, resume_id: int) -> Application:
        """Prepare and immediately submit (auto-submit mode)."""
        application = await self.prepare(job_id, resume_id=resume_id)
        assert application.id is not None
        async with self._session_factory() as session:
            app_repo = ApplicationRepository(session)
            await app_repo.update_status(application.id, ApplicationStatus.APPROVED)
            await session.commit()
        return await self.submit(application.id)

    async def _check_daily_cap(self, app_repo: ApplicationRepository) -> None:
        today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        submitted_today = await app_repo.count_submitted_since(today_start)
        if submitted_today >= self._prefs.max_applications_per_day:
            raise DailyCapReachedError(self._prefs.max_applications_per_day, submitted_today)

    async def _generate_cover_letter(self, job: Job) -> str:
        if self._llm is None:
            return ""
        prompt = (
            f"Write a concise, professional cover letter for this position.\n\n"
            f"Position: {job.title}\n"
            f"Company: {job.company}\n"
            f"Description:\n{job.description[:2000]}\n\n"
            f"Requirements:\n{', '.join(job.requirements[:20])}\n\n"
            f"Write 3-4 paragraphs. Be specific about why the candidate is a good fit. "
            f"Do not use generic filler. Return only the cover letter text, no headers."
        )
        return await self._llm.generate(
            prompt,
            system="You are a professional career coach writing cover letters. "
            "Be concise, specific, and avoid clichés.",
        )

    async def _generate_answers(self, job: Job) -> dict[str, str]:
        if self._llm is None:
            return {}
        prompt = (
            f"For this job application:\n"
            f"Position: {job.title} at {job.company}\n"
            f"Description: {job.description[:1500]}\n\n"
            f"Generate concise, professional answers (2-3 sentences each) for:\n"
            f'1. "Why do you want to work here?"\n'
            f'2. "Why are you qualified for this role?"\n\n'
            f"Return only the answers, numbered."
        )
        raw = await self._llm.generate(prompt, system="Write concise, honest application answers.")
        return {
            "Why do you want to work here?": raw.split("2.")[0].replace("1.", "").strip()
            if "2." in raw
            else raw.strip(),
            "Why are you qualified for this role?": raw.split("2.")[1].strip()
            if "2." in raw
            else "",
        }
