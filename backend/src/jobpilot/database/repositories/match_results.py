"""Match result persistence."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.database.orm import JobRow, MatchResultRow
from jobpilot.database.repositories.jobs import _to_domain as _job_to_domain
from jobpilot.domain.enums import MatchRecommendation
from jobpilot.domain.models import Job, MatchResult


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
        """Insert, or replace the existing result for the same (job, resume) pair.

        A single INSERT … ON CONFLICT DO UPDATE, so two matchers scoring the same
        job concurrently can't trip the unique constraint.
        """
        values = {
            "job_id": result.job_id,
            "resume_id": result.resume_id,
            "score": result.score,
            "matched_skills": result.matched_skills,
            "missing_skills": result.missing_skills,
            "recommendation": result.recommendation.value,
            "reasoning": result.reasoning,
            "llm_model": result.llm_model,
            "created_at": result.created_at,
        }
        stmt = sqlite_insert(MatchResultRow).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[MatchResultRow.job_id, MatchResultRow.resume_id],
            set_={key: stmt.excluded[key] for key in values if key not in ("job_id", "resume_id")},
        )
        await self._session.execute(stmt)
        row = await self._session.scalar(
            select(MatchResultRow)
            .where(
                MatchResultRow.job_id == result.job_id,
                MatchResultRow.resume_id == result.resume_id,
            )
            .execution_options(populate_existing=True)
        )
        assert row is not None
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

    async def list_with_jobs(
        self, resume_id: int, *, min_score: int = 0
    ) -> list[tuple[Job, MatchResult]]:
        """Every (job, match) pair for a resume in one query — input for ranking.

        Not truncated: the composite rank can promote a job the LLM scored
        lower, so capping by LLM score first would hide it.
        """
        rows = await self._session.execute(
            select(JobRow, MatchResultRow)
            .join(MatchResultRow, MatchResultRow.job_id == JobRow.id)
            .where(MatchResultRow.resume_id == resume_id, MatchResultRow.score >= min_score)
        )
        return [(_job_to_domain(job), _to_domain(match)) for job, match in rows.all()]

    async def count_for_resume(self, resume_id: int, *, min_score: int = 0) -> int:
        result = await self._session.scalar(
            select(func.count())
            .select_from(MatchResultRow)
            .where(MatchResultRow.resume_id == resume_id, MatchResultRow.score >= min_score)
        )
        return result or 0

    async def unmatched_job_ids(self, resume_id: int, *, limit: int = 500) -> list[int]:
        """IDs of jobs that have no match result for this resume yet."""
        matched = select(MatchResultRow.job_id).where(MatchResultRow.resume_id == resume_id)
        rows = await self._session.scalars(
            select(JobRow.id).where(JobRow.id.not_in(matched)).order_by(JobRow.id).limit(limit)
        )
        return list(rows)
