"""Match result persistence."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.database.orm import MatchResultRow
from jobpilot.domain.enums import MatchRecommendation
from jobpilot.domain.models import MatchResult


def _to_domain(row: MatchResultRow) -> MatchResult:
    return MatchResult(
        id=row.id,
        job_id=row.job_id,
        resume_id=row.resume_id,
        score=row.score,
        matched_skills=row.matched_skills,
        missing_skills=row.missing_skills,
        recommendation=MatchRecommendation(row.recommendation),
        reasoning=row.reasoning,
        llm_model=row.llm_model,
        created_at=row.created_at,
    )


class MatchResultRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, result: MatchResult) -> MatchResult:
        """Insert, or replace the existing result for the same (job, resume) pair."""
        existing = await self._session.scalar(
            select(MatchResultRow).where(
                MatchResultRow.job_id == result.job_id,
                MatchResultRow.resume_id == result.resume_id,
            )
        )
        if existing is not None:
            existing.score = result.score
            existing.matched_skills = result.matched_skills
            existing.missing_skills = result.missing_skills
            existing.recommendation = result.recommendation.value
            existing.reasoning = result.reasoning
            existing.llm_model = result.llm_model
            existing.created_at = result.created_at
            await self._session.flush()
            return _to_domain(existing)

        row = MatchResultRow(
            job_id=result.job_id,
            resume_id=result.resume_id,
            score=result.score,
            matched_skills=result.matched_skills,
            missing_skills=result.missing_skills,
            recommendation=result.recommendation.value,
            reasoning=result.reasoning,
            llm_model=result.llm_model,
            created_at=result.created_at,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_domain(row)

    async def get_for_job(self, job_id: int, resume_id: int) -> MatchResult | None:
        row = await self._session.scalar(
            select(MatchResultRow).where(
                MatchResultRow.job_id == job_id, MatchResultRow.resume_id == resume_id
            )
        )
        return _to_domain(row) if row else None

    async def list_for_resume(
        self, resume_id: int, *, min_score: int = 0, limit: int = 200
    ) -> list[MatchResult]:
        rows = await self._session.scalars(
            select(MatchResultRow)
            .where(MatchResultRow.resume_id == resume_id, MatchResultRow.score >= min_score)
            .order_by(MatchResultRow.score.desc())
            .limit(limit)
        )
        return [_to_domain(row) for row in rows]

    async def unmatched_job_ids(self, resume_id: int, *, limit: int = 500) -> list[int]:
        """IDs of jobs that have no match result for this resume yet."""
        from jobpilot.database.orm import JobRow

        matched = select(MatchResultRow.job_id).where(MatchResultRow.resume_id == resume_id)
        rows = await self._session.scalars(
            select(JobRow.id).where(JobRow.id.not_in(matched)).limit(limit)
        )
        return list(rows)
