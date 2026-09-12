"""Application orchestration: prepare materials, approve, and track submission.

Central coordination point that enforces every safety check:
- Duplicate prevention (same job_id, or same company+title across sources)
- Blacklisted companies are never prepared, even if scraped before blacklisting
- Daily cap (max_applications_per_day), counted against the user's local day
- LLM-generated cover letter and answers grounded in the actual resume
- Human approval is mandatory: nothing reaches a browser until APPROVED, and
  only the human submits. JobPilot records SUBMITTED solely on their say-so.
"""

from __future__ import annotations

import logging
from datetime import datetime, time

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.config.preferences import ApplicantProfile, UserPreferences
from jobpilot.database.repositories import (
    ApplicationRepository,
    DailyCapReachedError,
    DuplicateApplicationError,
    JobRepository,
    PlatformSessionRepository,
    ResumeRepository,
)
from jobpilot.domain.enums import ApplicationStatus, JobSource, SessionStatus
from jobpilot.domain.models import Application, Job, ResumeProfile
from jobpilot.llm.provider import LLMProvider

logger = logging.getLogger(__name__)

WHY_COMPANY_QUESTION = "Why do you want to work here?"
WHY_QUALIFIED_QUESTION = "Why are you qualified for this role?"

_COVER_LETTER_SYSTEM = (
    "You are a professional career coach writing cover letters. Be concise, specific, "
    "and honest. Only use facts from the candidate profile you are given: never invent "
    "employers, degrees, certifications, years of experience, or skills. Never output "
    "placeholders such as [Your Name] or [Company Address]."
)

_ANSWERS_SYSTEM = (
    "You write concise, honest job-application answers. Only use facts from the "
    "candidate profile. Respond with JSON only."
)

_ANSWERS_SCHEMA = {
    "type": "object",
    "properties": {
        "why_company": {"type": "string"},
        "why_qualified": {"type": "string"},
    },
    "required": ["why_company", "why_qualified"],
}


class BlacklistedCompanyError(ValueError):
    def __init__(self, company: str) -> None:
        super().__init__(f"{company!r} is on your blacklist_companies list")
        self.company = company


class IncompleteProfileError(ValueError):
    """Applicant details needed to fill any application are missing."""


def local_day_start(now: datetime | None = None) -> datetime:
    """Midnight of the user's local day, as an aware datetime.

    The daily cap is a promise about the user's day; resetting it at UTC
    midnight would reset at 08:00 in Manila.
    """
    local_now = now if now is not None else datetime.now().astimezone()
    if local_now.tzinfo is None:
        local_now = local_now.astimezone()
    return datetime.combine(local_now.date(), time.min, tzinfo=local_now.tzinfo)


def _candidate_summary(applicant: ApplicantProfile, profile: ResumeProfile) -> str:
    lines = [
        f"- Name: {applicant.name or '(not provided)'}",
        f"- Technologies: {', '.join(profile.technologies) or '(none listed)'}",
        f"- Skills: {', '.join(profile.skills[:25]) or '(none listed)'}",
        f"- Years of experience: {profile.years_experience or 'not stated'}",
        f"- Projects: {'; '.join(profile.projects[:5]) or '(none listed)'}",
        f"- Education: {'; '.join(profile.education[:3]) or '(none listed)'}",
    ]
    return "\n".join(lines)


def missing_applicant_fields(applicant: ApplicantProfile) -> list[str]:
    """Fields every application form needs; refuse to open a form without them."""
    return [name for name in ("name", "email") if not getattr(applicant, name).strip()]


class ApplicationService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        preferences: UserPreferences,
        llm: LLMProvider | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._prefs = preferences
        self._llm = llm

    async def prepare(self, job_id: int, *, resume_id: int) -> Application:
        """Validate, generate materials, and create a PENDING_REVIEW application."""
        async with self._session_factory() as session:
            job = await JobRepository(session).get(job_id)
            if job is None:
                raise ValueError(f"Job {job_id} not found")
            resume = await ResumeRepository(session).get(resume_id)
            if resume is None:
                raise ValueError(f"Resume {resume_id} not found")
            if self._prefs.is_company_blacklisted(job.company):
                raise BlacklistedCompanyError(job.company)

            app_repo = ApplicationRepository(session)
            if await app_repo.exists_for_job(job_id):
                raise DuplicateApplicationError(job_id)
            if await app_repo.exists_for_company_position(job.company, job.title):
                raise DuplicateApplicationError(
                    job_id,
                    reason=f"Already applied to {job.title!r} at {job.company!r} "
                    "(via another listing)",
                )
            await self._check_daily_cap(app_repo)

        # Generate outside any DB transaction: LLM calls take tens of seconds.
        notes: list[str] = []
        cover_letter = await self._generate_cover_letter(job, resume.profile, notes)
        answers = await self._generate_answers(job, resume.profile, notes)

        async with self._session_factory() as session:
            app_repo = ApplicationRepository(session)
            try:
                application = await app_repo.create(
                    Application(
                        job_id=job_id,
                        resume_id=resume_id,
                        status=ApplicationStatus.PENDING_REVIEW,
                        cover_letter=cover_letter,
                        answers=answers,
                        notes="\n".join(notes),
                    )
                )
                await session.commit()
            except DuplicateApplicationError:
                await session.rollback()
                raise
        logger.info(
            "Application %s prepared for %s at %s (pending review)",
            application.id,
            job.title,
            job.company,
        )
        return application

    async def approve(self, application_id: int) -> Application:
        return await self._transition(application_id, ApplicationStatus.APPROVED)

    async def reject(self, application_id: int, *, reason: str | None = None) -> Application:
        return await self._transition(application_id, ApplicationStatus.REJECTED, notes=reason)

    async def claim_for_autofill(self, application_id: int) -> tuple[Application, Job]:
        """Reserve an approved application for a browser session.

        Moves it to AWAITING_CONFIRMATION atomically (so two workers can't open
        the same form) and only while under today's cap.
        """
        missing = missing_applicant_fields(self._prefs.applicant)
        if missing:
            raise IncompleteProfileError(
                "Fill in applicant."
                + ", applicant.".join(missing)
                + " in config/config.yaml before applying."
            )
        async with self._session_factory() as session:
            app_repo = ApplicationRepository(session)
            try:
                application = await app_repo.claim_for_autofill(
                    application_id,
                    daily_cap=self._prefs.max_applications_per_day,
                    day_start=local_day_start(),
                    platform_caps={
                        name: settings.max_applications_per_day
                        for name, settings in self._prefs.platforms.items()
                        if settings.max_applications_per_day is not None
                    },
                )
                job = await JobRepository(session).get(application.job_id)
                if job is None:  # FK makes this impossible; fail loudly if it happens
                    raise ValueError(f"Job {application.job_id} not found")
                await session.commit()
            except Exception:
                await session.rollback()
                raise
        return application, job

    async def record_autofill(
        self, application_id: int, *, screenshot_path: str | None, summary: dict[str, object]
    ) -> None:
        async with self._session_factory() as session:
            repo = ApplicationRepository(session)
            if screenshot_path:
                await repo.set_screenshot(application_id, screenshot_path)
            await repo.log_event(application_id, "autofilled", summary)
            await session.commit()

    async def mark_submitted(self, application_id: int) -> Application:
        """The human confirmed they submitted the form. The only path to SUBMITTED."""
        return await self._transition(
            application_id,
            ApplicationStatus.SUBMITTED,
            # Replace notes like "not submitted yet" left by an earlier session.
            notes="Submitted — confirmed by you in `jobpilot apply`.",
            payload={"confirmed_by": "user"},
        )

    async def release(self, application_id: int, *, notes: str | None = None) -> Application:
        """Put a claimed application back in the approved queue (not submitted)."""
        return await self._transition(application_id, ApplicationStatus.APPROVED, notes=notes)

    async def return_to_review(self, application_id: int, *, notes: str) -> Application:
        return await self._transition(application_id, ApplicationStatus.PENDING_REVIEW, notes=notes)

    async def propose_answers(self, application_id: int, proposed: dict[str, str]) -> Application:
        """Send an open application back to review with LLM-proposed answers.

        Proposed answers are never typed into a form directly: the human
        reviews (and edits) them first, then approves again.
        """
        async with self._session_factory() as session:
            repo = ApplicationRepository(session)
            try:
                current = await repo.get(application_id)
                if current is None:
                    raise ValueError(f"Application {application_id} not found")
                await repo.transition(
                    application_id,
                    ApplicationStatus.PENDING_REVIEW,
                    notes=(
                        f"JobPilot proposed {len(proposed)} answer(s) for questions on the "
                        "form. Review and edit them, then approve again."
                    ),
                    payload={"proposed_questions": sorted(proposed)},
                )
                updated = await repo.update_materials(
                    application_id, answers={**current.answers, **proposed}
                )
                await session.commit()
            except Exception:
                await session.rollback()
                raise
        return updated

    async def record_platform_session(
        self, platform: JobSource, status: SessionStatus, detail: str = ""
    ) -> None:
        async with self._session_factory() as session:
            await PlatformSessionRepository(session).record(platform, status, detail=detail)
            await session.commit()

    async def mark_failed(self, application_id: int, *, reason: str) -> Application:
        return await self._transition(application_id, ApplicationStatus.FAILED, notes=reason)

    async def _transition(
        self,
        application_id: int,
        target: ApplicationStatus,
        *,
        notes: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> Application:
        async with self._session_factory() as session:
            try:
                updated = await ApplicationRepository(session).transition(
                    application_id, target, notes=notes, payload=payload
                )
                await session.commit()
            except Exception:
                await session.rollback()
                raise
        logger.info("Application %s -> %s", application_id, target.value)
        return updated

    async def _check_daily_cap(self, app_repo: ApplicationRepository) -> None:
        used = await app_repo.count_toward_cap(local_day_start())
        if used >= self._prefs.max_applications_per_day:
            raise DailyCapReachedError(self._prefs.max_applications_per_day, used)

    async def _generate_cover_letter(
        self, job: Job, profile: ResumeProfile, notes: list[str]
    ) -> str:
        if self._llm is None:
            return ""
        prompt = (
            "Write a concise, professional cover letter for this position.\n\n"
            f"Candidate profile (the ONLY facts you may use about the candidate):\n"
            f"{_candidate_summary(self._prefs.applicant, profile)}\n\n"
            f"Position: {job.title}\n"
            f"Company: {job.company}\n"
            f"Description:\n{job.description[:2000]}\n\n"
            "Write 3-4 paragraphs connecting the candidate's real skills to this role. "
            "If the profile lacks something the job needs, don't claim it. "
            "Return only the letter body, signed with the candidate's name if provided."
        )
        try:
            return (await self._llm.generate(prompt, system=_COVER_LETTER_SYSTEM)).strip()
        except Exception as exc:  # noqa: BLE001 - materials are best-effort; review catches gaps
            logger.warning("Cover letter generation failed for job %s: %s", job.id, exc)
            notes.append(f"Cover letter generation failed ({exc}); write one before approving.")
            return ""

    async def _generate_answers(
        self, job: Job, profile: ResumeProfile, notes: list[str]
    ) -> dict[str, str]:
        if self._llm is None:
            return {}
        prompt = (
            f"Candidate profile (the ONLY facts you may use about the candidate):\n"
            f"{_candidate_summary(self._prefs.applicant, profile)}\n\n"
            f"Position: {job.title} at {job.company}\n"
            f"Description: {job.description[:1500]}\n\n"
            "Write 2-3 sentence answers for:\n"
            f'- why_company: "{WHY_COMPANY_QUESTION}"\n'
            f'- why_qualified: "{WHY_QUALIFIED_QUESTION}"'
        )
        try:
            raw = await self._llm.generate_json(
                prompt, system=_ANSWERS_SYSTEM, schema=_ANSWERS_SCHEMA
            )
        except Exception as exc:  # noqa: BLE001 - best-effort, like the cover letter
            logger.warning("Answer generation failed for job %s: %s", job.id, exc)
            notes.append(f"Answer generation failed ({exc}).")
            return {}
        answers = {
            WHY_COMPANY_QUESTION: str(raw.get("why_company") or "").strip(),
            WHY_QUALIFIED_QUESTION: str(raw.get("why_qualified") or "").strip(),
        }
        return {question: answer for question, answer in answers.items() if answer}
