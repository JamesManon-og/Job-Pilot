"""Trigger scrape/match runs from the dashboard (fire-and-forget background tasks).

Both take the same cross-process pipeline lock as the CLI, so clicking
"Scrape now" during a `jobpilot run` returns 409 instead of racing it.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from jobpilot.api.deps import AppState, StateDep
from jobpilot.locks import PIPELINE_LOCK, LockBusyError, process_lock
from jobpilot.scrapers import ScrapeRunner, all_scrapers

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/actions", tags=["actions"])


class ActionAccepted(BaseModel):
    status: str = "started"
    detail: str = ""


def _ensure_pipeline_idle(state: AppState) -> None:
    try:
        with process_lock(PIPELINE_LOCK, state.settings.locks_dir):
            pass
    except LockBusyError as exc:
        raise HTTPException(409, str(exc)) from None


async def _scrape_all(state: AppState) -> None:
    try:
        with process_lock(PIPELINE_LOCK, state.settings.locks_dir):
            runner = ScrapeRunner(state.session_factory, state.preferences())
            for source in all_scrapers():
                await runner.run(source)  # records failures itself; never raises per source
    except LockBusyError as exc:
        logger.warning("Scrape skipped: %s", exc)
    except Exception:  # noqa: BLE001 - background task: log, don't crash the server
        logger.exception("Background scrape failed")


async def _match_all(state: AppState) -> None:
    from jobpilot.llm import OllamaClient
    from jobpilot.matcher import MatchEngine, MatchingAbortedError, MatchService

    llm = OllamaClient(state.settings.ollama_base_url, state.settings.ollama_model)
    if not (await llm.is_available() and await llm.has_model()):
        logger.error("Cannot run matching: Ollama/model unavailable")
        return
    try:
        with process_lock(PIPELINE_LOCK, state.settings.locks_dir):
            service = MatchService(
                state.session_factory, MatchEngine(llm, model_name=llm.model), state.preferences()
            )
            await service.match_unscored(limit=500)
    except LockBusyError as exc:
        logger.warning("Matching skipped: %s", exc)
    except MatchingAbortedError as exc:
        logger.error("Background matching stopped early: %s", exc)
    except Exception:  # noqa: BLE001
        logger.exception("Background matching failed")


@router.post("/scrape", response_model=ActionAccepted)
async def trigger_scrape(background: BackgroundTasks, state: StateDep) -> ActionAccepted:
    _ensure_pipeline_idle(state)
    sources = ", ".join(s.value for s in all_scrapers())
    background.add_task(_scrape_all, state)
    return ActionAccepted(detail=f"Scraping: {sources}")


@router.post("/match", response_model=ActionAccepted)
async def trigger_match(background: BackgroundTasks, state: StateDep) -> ActionAccepted:
    _ensure_pipeline_idle(state)
    background.add_task(_match_all, state)
    return ActionAccepted(detail="Matching unscored jobs against the active resume")
