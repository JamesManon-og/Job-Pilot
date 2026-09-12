"""Dashboard overview statistics."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select

from jobpilot.api.deps import SessionDep, StateDep
from jobpilot.applications import local_day_start
from jobpilot.control import is_paused
from jobpilot.database.orm import ApplicationRow, JobRow, ResumeRow, ScrapeRunRow
from jobpilot.database.repositories import (
    ApplicationRepository,
    JobRepository,
    MatchResultRepository,
)

router = APIRouter(prefix="/api/stats", tags=["stats"])


class Stats(BaseModel):
    total_jobs: int
    total_matched: int
    strong_matches: int
    applications_by_status: dict[str, int]
    jobs_by_status: dict[str, int]
    applications_today: int
    daily_cap: int
    paused: bool
    resumes: int
    last_scrape: str | None
    scrape_failures_recent: int


@router.get("", response_model=Stats)
async def get_stats(session: SessionDep, state: StateDep) -> Stats:
    total_jobs = (await session.scalar(select(func.count()).select_from(JobRow))) or 0
    # Matches are per resume; counting every resume's rows double-counts jobs.
    active_resume_id = await session.scalar(
        select(ResumeRow.id).where(ResumeRow.is_active.is_(True))
    )
    total_matched = strong_matches = 0
    if active_resume_id is not None:
        matches = MatchResultRepository(session)
        total_matched = await matches.count_for_resume(active_resume_id)
        strong_matches = await matches.count_for_resume(active_resume_id, min_score=80)
    resumes = (await session.scalar(select(func.count()).select_from(ResumeRow))) or 0

    status_rows = await session.execute(
        select(ApplicationRow.status, func.count()).group_by(ApplicationRow.status)
    )
    applications_by_status = {status: count for status, count in status_rows.all()}

    last_scrape_at = await session.scalar(
        select(func.max(ScrapeRunRow.started_at)).select_from(ScrapeRunRow)
    )
    recent_ids = (
        select(ScrapeRunRow.id).order_by(ScrapeRunRow.started_at.desc()).limit(20)
    ).subquery()
    recent_failures = (
        await session.scalar(
            select(func.count())
            .select_from(ScrapeRunRow)
            .where(ScrapeRunRow.id.in_(select(recent_ids.c.id)), ScrapeRunRow.status == "failed")
        )
    ) or 0

    preferences = state.preferences()
    return Stats(
        jobs_by_status=await JobRepository(session).count_by_status(),
        applications_today=await ApplicationRepository(session).count_toward_cap(local_day_start()),
        daily_cap=preferences.max_applications_per_day,
        paused=is_paused(state.settings.data_dir),
        total_jobs=total_jobs,
        total_matched=total_matched,
        strong_matches=strong_matches,
        applications_by_status=applications_by_status,
        resumes=resumes,
        last_scrape=last_scrape_at.isoformat() if last_scrape_at else None,
        scrape_failures_recent=recent_failures,
    )
