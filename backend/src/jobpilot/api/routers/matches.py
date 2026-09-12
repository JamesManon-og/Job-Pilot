"""Ranked matches for the active resume."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from jobpilot.api.deps import SessionDep, StateDep
from jobpilot.database.repositories import MatchResultRepository, ResumeRepository
from jobpilot.domain.models import Job, MatchResult
from jobpilot.matcher.ranking import rank_jobs

router = APIRouter(prefix="/api/matches", tags=["matches"])


class RankedMatch(BaseModel):
    job: Job
    match: MatchResult
    composite_score: float


@router.get("", response_model=list[RankedMatch])
async def list_matches(
    session: SessionDep,
    state: StateDep,
    min_score: int = 0,
    top: int = 50,
) -> list[RankedMatch]:
    resume = await ResumeRepository(session).get_active()
    if resume is None or resume.id is None:
        raise HTTPException(status_code=409, detail="No active resume imported yet")

    pairs = await MatchResultRepository(session).list_with_jobs(resume.id, min_score=min_score)
    preferences = state.preferences()
    ranked = rank_jobs(
        pairs,
        weights=preferences.ranking_weights,
        preferred_salary_min=preferences.preferred_salary_min,
        preferred_technologies=preferences.preferred_technologies,
    )
    return [
        RankedMatch(job=r.job, match=r.match, composite_score=r.composite_score)
        for r in ranked[: max(0, min(top, 200))]
    ]
