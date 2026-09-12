"""Search one platform: queries → listings → dedupe → enrich → filter → persist.

Guarantees (each covered by tests):
- Failure isolation: a platform that errors, logs out, or hits a challenge
  records a failed run and stops; the caller moves on to the next platform.
- Already-processed jobs are recognized from the results page and never
  re-enriched (no detail-page load, no LLM work later).
- One bad listing never poisons the run: per-job commits, rollback on DB
  errors, and a circuit breaker for platforms that fail repeatedly.
- Interrupted runs are closed on the next start; progress is never lost.
- Walls are never bypassed: they mark the session NEEDS_LOGIN / BLOCKED.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from playwright.async_api import BrowserContext
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.config.preferences import UserPreferences
from jobpilot.database.repositories import (
    JobRepository,
    PlatformSessionRepository,
    ScrapeRunRepository,
)
from jobpilot.domain.enums import EmploymentType, JobSource, RemoteType, SessionStatus
from jobpilot.domain.models import Job, ScrapeRun
from jobpilot.platforms.base import (
    ChallengeError,
    PlatformAdapter,
    PlatformError,
    SearchQuery,
    SessionExpiredError,
)
from jobpilot.platforms.browser import BrowserProfiles
from jobpilot.platforms.registry import get_adapter_class

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_FAILURES = 5
MATCH_EVERYTHING = "*"


@dataclass
class SearchStats:
    found: int = 0
    new: int = 0
    known: int = 0  # already in the database: skipped without re-enriching
    filtered: int = 0
    failed: int = 0


class SearchRunner:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        preferences: UserPreferences,
        *,
        profiles: BrowserProfiles | None = None,
        adapters: dict[JobSource, PlatformAdapter] | None = None,
        paused: Callable[[], bool] = lambda: False,
    ) -> None:
        self._factory = session_factory
        self._prefs = preferences
        self._profiles = profiles
        self._adapters = dict(adapters or {})
        self._paused = paused
        self.last_stats: dict[JobSource, SearchStats] = {}

    def adapter(self, platform: JobSource) -> PlatformAdapter:
        if platform not in self._adapters:
            cls = get_adapter_class(platform)
            self._adapters[platform] = cls(self._prefs.platform(platform.value))
        return self._adapters[platform]

    def queries_for(self, adapter: PlatformAdapter) -> list[SearchQuery]:
        settings = self._prefs.platform(adapter.platform.value)
        limit = settings.max_results_per_query or self._prefs.search.max_results_per_query
        terms = self._prefs.search_queries()
        if not terms and not adapter.uses_browser:
            terms = [MATCH_EVERYTHING]  # API feeds: keep everything, as before queries existed
        return [
            SearchQuery(
                keywords=term,
                location=self._prefs.search.location,
                remote_only=self._prefs.remote_only,
                max_results=limit,
                posted_within_days=self._prefs.search.posted_within_days,
            )
            for term in terms
        ]

    async def run(self, platform: JobSource) -> ScrapeRun:
        """Search one platform. Always records a ScrapeRun, never raises.

        Callers must hold the pipeline lock: stale RUNNING rows for this
        platform are closed as interrupted on start.
        """
        async with self._factory() as session:
            runs = ScrapeRunRepository(session)
            interrupted = await runs.mark_interrupted(platform)
            if interrupted:
                logger.warning("Closed %d interrupted %s run(s)", interrupted, platform.value)
            run = await runs.start(platform)
            await session.commit()
        assert run.id is not None

        stats = SearchStats()
        self.last_stats[platform] = stats
        error: str | None = None
        try:
            adapter = self.adapter(platform)
            queries = self.queries_for(adapter)
            if not queries:
                raise PlatformError(
                    platform,
                    "nothing to search for — set search.queries (or preferred_job_titles) "
                    "in config.yaml",
                )
            if adapter.uses_browser:
                if self._profiles is None:
                    raise PlatformError(platform, "needs a browser profile; none configured")
                async with self._profiles.open(platform) as browser:
                    await self._check_session(adapter, browser)
                    error = await self._collect(adapter, queries, browser, stats)
            else:
                error = await self._collect(adapter, queries, None, stats)
        except SessionExpiredError as exc:
            await self._record_session(platform, SessionStatus.NEEDS_LOGIN, str(exc))
            error = str(exc)
        except ChallengeError as exc:
            await self._record_session(platform, SessionStatus.BLOCKED, str(exc))
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - one platform must never take down the others
            logger.exception("Search on %s failed", platform.value)
            error = f"{type(exc).__name__}: {exc}"

        async with self._factory() as session:
            finished = await ScrapeRunRepository(session).finish(
                run.id, jobs_found=stats.found, jobs_new=stats.new, error=error
            )
            await session.commit()
        assert finished is not None
        logger.info(
            "Search %s: %d found, %d new, %d already known, %d filtered, %d failed — %s",
            platform.value,
            stats.found,
            stats.new,
            stats.known,
            stats.filtered,
            stats.failed,
            finished.status.value,
        )
        return finished

    async def _record_session(
        self, platform: JobSource, status: SessionStatus, detail: str = ""
    ) -> None:
        async with self._factory() as session:
            await PlatformSessionRepository(session).record(platform, status, detail=detail)
            await session.commit()

    async def _check_session(self, adapter: PlatformAdapter, browser: BrowserContext) -> None:
        status = await adapter.session_status(browser)
        await self._record_session(adapter.platform, status)
        if status is SessionStatus.BLOCKED:
            raise ChallengeError(adapter.platform, "checkpoint")
        if adapter.requires_login_to_search and status is not SessionStatus.LOGGED_IN:
            raise SessionExpiredError(adapter.platform)

    def _excluded(self, job: Job, *, detailed: bool) -> str | None:
        if self._prefs.is_company_blacklisted(job.company):
            return f"blacklisted company {job.company!r}"
        keyword = self._prefs.excluded_keyword_in(job.title, job.description if detailed else "")
        if keyword:
            return f"excluded keyword {keyword!r}"
        if not detailed:
            return None
        if self._prefs.remote_only and job.remote is RemoteType.ONSITE:
            return "onsite (remote_only)"
        wanted = {t.strip().lower() for t in self._prefs.employment_types if t.strip()}
        if (
            wanted
            and job.employment_type is not EmploymentType.UNKNOWN
            and job.employment_type.value not in wanted
        ):
            return f"employment type {job.employment_type.value}"
        return None

    async def _collect(
        self,
        adapter: PlatformAdapter,
        queries: list[SearchQuery],
        browser: BrowserContext | None,
        stats: SearchStats,
    ) -> str | None:
        """Run every query. Returns a partial-failure message, or None."""
        problems: list[str] = []
        consecutive_failures = 0
        async with self._factory() as session:
            jobs = JobRepository(session)
            for query in queries:
                if self._paused():
                    problems.append("paused")
                    break
                try:
                    async for listing in adapter.search(query, browser):
                        if self._paused():
                            problems.append("paused")
                            break
                        stats.found += 1
                        reason = self._excluded(listing, detailed=False)
                        if reason:
                            stats.filtered += 1
                            logger.debug("Filtered %s: %s", listing.title, reason)
                            continue
                        existing = await jobs.find_existing(listing)
                        if existing is not None and existing.id is not None:
                            await jobs.touch_seen(existing.id)
                            await session.commit()
                            stats.known += 1
                            continue
                        try:
                            # Re-derive identity: adapters may have edited fields.
                            job = (await adapter.enrich(listing, browser)).reidentified()
                        except (SessionExpiredError, ChallengeError):
                            raise
                        except Exception as exc:  # noqa: BLE001 - one detail page ≠ the run
                            stats.failed += 1
                            consecutive_failures += 1
                            logger.warning("Could not load %s: %s", listing.application_url, exc)
                            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                                raise PlatformError(
                                    adapter.platform,
                                    f"{consecutive_failures} postings in a row failed to load "
                                    f"(last: {exc})",
                                ) from exc
                            continue
                        consecutive_failures = 0
                        reason = self._excluded(job, detailed=True)
                        if reason:
                            stats.filtered += 1
                            continue
                        try:
                            _, created = await jobs.upsert(job)
                            await session.commit()  # per job: interruption keeps progress
                        except SQLAlchemyError:
                            await session.rollback()
                            stats.failed += 1
                            logger.exception("Could not save %r @ %r", job.title, job.company)
                            continue
                        if created:
                            stats.new += 1
                except (SessionExpiredError, ChallengeError, PlatformError):
                    raise
                except Exception as exc:  # noqa: BLE001 - next query may still work
                    await session.rollback()
                    logger.exception("Query %r on %s failed", query.keywords, adapter.platform)
                    problems.append(f"query {query.keywords!r}: {type(exc).__name__}: {exc}")
                if problems and problems[-1] == "paused":
                    break
        return "; ".join(problems) or None
