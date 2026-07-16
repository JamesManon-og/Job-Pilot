"""Trigger scrape/match runs from the dashboard (fire-and-forget background tasks)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel

from jobpilot.api.deps import AppState, StateDep
from jobpilot.scrapers import ScrapeRunner, all_scrapers

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/actions", tags=["actions"])


class ActionAccepted(BaseModel):
    status: str = "started"
    detail: str = ""


async def _scrape_all(state: AppState) -> None:
    runner = ScrapeRunner(state.session_factory, state.preferences())
    for source in all_scrapers():
        try:
            await runner.run(source)
        except Exception:  # noqa: BLE001
            logger.exception("Background scrape of %s failed", source)


async def _match_all(state: AppState) -> None:
    from jobpilot.llm import OllamaClient
    from jobpilot.matcher import MatchEngine, MatchService

    llm = OllamaClient(state.settings.ollama_base_url, state.settings.ollama_model)
    if not (await llm.is_available() and await llm.has_model()):
        logger.error("Cannot run matching: Ollama/model unavailable")
        return
    service = MatchService(
        state.session_factory, MatchEngine(llm, model_name=llm.model), state.preferences()
    )
    try:
        await service.match_unscored(limit=500)
    except Exception:  # noqa: BLE001
        logger.exception("Background matching failed")


@router.post("/scrape", response_model=ActionAccepted)
async def trigger_scrape(background: BackgroundTasks, state: StateDep) -> ActionAccepted:
    sources = ", ".join(s.value for s in all_scrapers())
    background.add_task(_scrape_all, state)
    return ActionAccepted(detail=f"Scraping: {sources}")


@router.post("/match", response_model=ActionAccepted)
async def trigger_match(background: BackgroundTasks, state: StateDep) -> ActionAccepted:
    background.add_task(_match_all, state)
    return ActionAccepted(detail="Matching unscored jobs against the active resume")
