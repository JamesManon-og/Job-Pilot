"""Scraper status."""

from __future__ import annotations

from fastapi import APIRouter

from jobpilot.api.deps import SessionDep
from jobpilot.database.repositories import ScrapeRunRepository
from jobpilot.domain.models import ScrapeRun

router = APIRouter(prefix="/api/scrape-runs", tags=["scrape-runs"])


@router.get("", response_model=list[ScrapeRun])
async def list_scrape_runs(session: SessionDep, limit: int = 20) -> list[ScrapeRun]:
    return await ScrapeRunRepository(session).list_recent(limit=min(limit, 100))
