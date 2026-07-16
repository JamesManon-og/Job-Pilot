"""Matching orchestration: score unmatched jobs, produce ranked lists."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.config.preferences import UserPreferences
from jobpilot.database.repositories import (
    JobRepository,
    MatchResultRepository,
    ResumeRepository,
)
from jobpilot.domain.models import MatchResult
from jobpilot.matcher.engine import MatchEngine
from jobpilot.matcher.ranking import RankedJob, rank_jobs

logger = logging.getLogger(__name__)


class NoActiveResumeError(Exception):
    def __init__(self) -> None:
        super().__init__("No active resume. Import one first: jobpilot resume import <path.pdf>")


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

    async def match_unscored(self, *, limit: int = 100) -> list[MatchResult]:
        """Score every job that doesn't have a match result for the active resume."""
        results: list[MatchResult] = []
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
                result = await self._engine.match(job, resume.profile, resume_id=resume.id)
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

    async def ranked(self, *, min_score: int = 0, top: int = 20) -> list[RankedJob]:
        async with self._session_factory() as session:
            resume = await ResumeRepository(session).get_active()
            if resume is None or resume.id is None:
                raise NoActiveResumeError
            matches = await MatchResultRepository(session).list_for_resume(
                resume.id, min_score=min_score
            )
            jobs_repo = JobRepository(session)
            pairs = []
            for match in matches:
                job = await jobs_repo.get(match.job_id)
                if job is not None:
                    pairs.append((job, match))
        ranked = rank_jobs(
            pairs,
            weights=self._preferences.ranking_weights,
            preferred_salary_min=self._preferences.preferred_salary_min,
            preferred_technologies=self._preferences.preferred_technologies,
        )
        return ranked[:top]
