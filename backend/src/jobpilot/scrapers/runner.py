"""Orchestrates a scrape: run scraper -> filter by preferences -> upsert -> bookkeeping."""

from __future__ import annotations

import logging

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio.session import AsyncSession

from jobpilot.config.preferences import UserPreferences
from jobpilot.database.repositories import JobRepository, ScrapeRunRepository
from jobpilot.domain.enums import JobSource, RemoteType
from jobpilot.domain.models import Job, ScrapeRun
from jobpilot.scrapers.registry import get_scraper

logger = logging.getLogger(__name__)


class ScrapeRunner:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        preferences: UserPreferences,
    ) -> None:
        self._session_factory = session_factory
        self._preferences = preferences

    def _passes_filters(self, job: Job) -> bool:
        if self._preferences.is_company_blacklisted(job.company):
            logger.info("Skipping blacklisted company: %s", job.company)
            return False
        if self._preferences.remote_only and job.remote is RemoteType.ONSITE:
            logger.debug("Skipping onsite job: %s @ %s", job.title, job.company)
            return False
        return True

    async def run(self, source: JobSource) -> ScrapeRun:
        """Scrape one source. Always records a ScrapeRun row, even on failure.

        Callers must hold the pipeline lock (see ``jobpilot.locks``): stale
        RUNNING rows for this source are closed as interrupted on start.
        """
        scraper = get_scraper(source)()
        async with self._session_factory() as session:
            runs = ScrapeRunRepository(session)
            jobs_repo = JobRepository(session)

            interrupted = await runs.mark_interrupted(source)
            if interrupted:
                logger.warning("Closed %d interrupted %s scrape run(s)", interrupted, source)
            run = await runs.start(source)
            assert run.id is not None
            await session.commit()

            found = 0
            new = 0
            failed_items = 0
            error: str | None = None
            try:
                async for job in scraper.scrape():
                    found += 1
                    if not self._passes_filters(job):
                        continue
                    try:
                        _, created = await jobs_repo.upsert(job)
                        # Commit per job: an interrupted scrape keeps its progress.
                        await session.commit()
                    except SQLAlchemyError:
                        # One bad listing must not poison the session or the run.
                        await session.rollback()
                        failed_items += 1
                        logger.exception(
                            "Could not save %s listing %r @ %r", source, job.title, job.company
                        )
                        continue
                    if created:
                        new += 1
            except Exception as exc:  # noqa: BLE001 - a failed source must not kill the run log
                await session.rollback()
                error = f"{type(exc).__name__}: {exc}"
                logger.exception("Scrape of %s failed", source)

            if failed_items:
                logger.warning("Scrape %s: %d listing(s) could not be saved", source, failed_items)
            finished = await runs.finish(run.id, jobs_found=found, jobs_new=new, error=error)
            await session.commit()
            assert finished is not None
            logger.info(
                "Scrape %s: %s found, %s new, status=%s",
                source,
                found,
                new,
                finished.status,
            )
            return finished
