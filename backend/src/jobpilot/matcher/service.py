"""Matching orchestration: score unmatched jobs, produce ranked lists."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.config.preferences import UserPreferences
from jobpilot.database.repositories import (
    ApplicationRepository,
    JobRepository,
    MatchResultRepository,
    ResumeRepository,
)
from jobpilot.domain.models import MatchResult
from jobpilot.llm.ollama import OllamaUnavailableError
from jobpilot.matcher.engine import MatchEngine
from jobpilot.matcher.ranking import RankedJob, rank_jobs

logger = logging.getLogger(__name__)

# After this many failures in a row the model is clearly broken (not one odd
# posting); stop instead of burning an hour of timeouts.
MAX_CONSECUTIVE_FAILURES = 5


class NoActiveResumeError(Exception):
    def __init__(self) -> None:
        super().__init__("No active resume. Import one first: jobpilot resume import <path.pdf>")


class MatchingAbortedError(Exception):
    """Matching stopped early. Results scored so far are already saved."""

    def __init__(self, reason: str, *, scored: list[MatchResult]) -> None:
        super().__init__(reason)
        self.scored = scored


class MatchService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        engine: MatchEngine,
        preferences: UserPreferences,
    ) -> None:
        self._session_factory = session_factory
        self._engine = engine
        self._preferences = preferences
        self.last_failures: dict[int, str] = {}

    async def match_unscored(self, *, limit: int = 100) -> list[MatchResult]:
        """Score every job that doesn't have a match result for the active resume.

        One job the model chokes on is logged and skipped (it stays unscored and
        is retried next run). If Ollama goes away mid-run, or keeps failing,
        raises MatchingAbortedError; everything scored before that is committed.
        """
        results: list[MatchResult] = []
        self.last_failures = {}
        consecutive_failures = 0
        async with self._session_factory() as session:
            resumes = ResumeRepository(session)
            resume = await resumes.get_active()
            if resume is None or resume.id is None:
                raise NoActiveResumeError
            jobs_repo = JobRepository(session)
            matches = MatchResultRepository(session)

            job_ids = await matches.unmatched_job_ids(resume.id, limit=limit)
            logger.info("Matching %d unscored jobs against resume %s", len(job_ids), resume.version)
            for index, job_id in enumerate(job_ids, 1):
                job = await jobs_repo.get(job_id)
                if job is None:
                    continue
                try:
                    result = await self._engine.match(job, resume.profile, resume_id=resume.id)
                except OllamaUnavailableError as exc:
                    raise MatchingAbortedError(str(exc), scored=results) from exc
                except Exception as exc:  # noqa: BLE001 - one bad job must not stop the batch
                    consecutive_failures += 1
                    self.last_failures[job_id] = f"{type(exc).__name__}: {exc}"
                    logger.warning(
                        "[%d/%d] could not score job %s: %s", index, len(job_ids), job_id, exc
                    )
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        raise MatchingAbortedError(
                            f"{consecutive_failures} jobs in a row failed to score "
                            f"(last error: {exc}). Check `jobpilot llm check`.",
                            scored=results,
                        ) from exc
                    continue
                consecutive_failures = 0
                await matches.upsert(result)
                await session.commit()  # commit per job: long LLM runs survive interruption
                results.append(result)
                logger.info(
                    "[%d/%d] %s @ %s -> %d (%s)",
                    index,
                    len(job_ids),
                    job.title,
                    job.company,
                    result.score,
                    result.recommendation.value,
                )
        return results

    async def match_one(self, job_id: int) -> MatchResult:
        async with self._session_factory() as session:
            resume = await ResumeRepository(session).get_active()
            if resume is None or resume.id is None:
                raise NoActiveResumeError
            job = await JobRepository(session).get(job_id)
            if job is None:
                raise ValueError(f"No job with id {job_id}")
            result = await self._engine.match(job, resume.profile, resume_id=resume.id)
            await MatchResultRepository(session).upsert(result)
            await session.commit()
            return result

    async def ranked(
        self, *, min_score: int = 0, top: int = 20, exclude_applied: bool = False
    ) -> list[RankedJob]:
        """Ranked matches for the active resume.

        `min_score` filters on the LLM score (0-100). With `exclude_applied`,
        jobs that already have an application (in any status) are dropped, so
        repeated pipeline runs move on to new jobs instead of re-trying old ones.
        Blacklisted companies are always excluded.
        """
        async with self._session_factory() as session:
            resume = await ResumeRepository(session).get_active()
            if resume is None or resume.id is None:
                raise NoActiveResumeError
            pairs = await MatchResultRepository(session).list_with_jobs(
                resume.id, min_score=min_score
            )
            applied = (
                await ApplicationRepository(session).job_ids_with_applications()
                if exclude_applied
                else set()
            )
        pairs = [
            (job, match)
            for job, match in pairs
            if job.id not in applied and not self._preferences.is_company_blacklisted(job.company)
        ]
        ranked = rank_jobs(
            pairs,
            weights=self._preferences.ranking_weights,
            preferred_salary_min=self._preferences.preferred_salary_min,
            preferred_technologies=self._preferences.preferred_technologies,
        )
        return ranked[: max(top, 0)]
