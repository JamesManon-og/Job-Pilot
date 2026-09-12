"""The `jobpilot run` pipeline: scrape → match → rank → prepare.

Lives outside the CLI so it can be tested. Every stage degrades gracefully:
a failed source doesn't stop the others, an unreachable LLM skips the stages
that need it, and one bad job never aborts a batch. Nothing here opens a
browser or submits anything — prepared applications wait for human review.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.applications import (
    ApplicationService,
    BlacklistedCompanyError,
    DailyCapReachedError,
)
from jobpilot.config.preferences import UserPreferences
from jobpilot.database.repositories import DuplicateApplicationError, ResumeRepository
from jobpilot.domain.enums import JobSource
from jobpilot.domain.models import ScrapeRun
from jobpilot.llm.provider import LLMProvider
from jobpilot.matcher import MatchEngine, MatchingAbortedError, MatchService
from jobpilot.matcher.ranking import RankedJob
from jobpilot.scrapers import ScrapeRunner

logger = logging.getLogger(__name__)


@dataclass
class PipelineReport:
    scrape_runs: list[ScrapeRun] = field(default_factory=list)
    scored: int = 0
    score_failures: int = 0
    candidates: list[RankedJob] = field(default_factory=list)
    prepared: list[int] = field(default_factory=list)  # application ids
    skipped: list[str] = field(default_factory=list)  # human-readable reasons
    stopped_early: str | None = None  # why later stages were skipped

    @property
    def new_jobs(self) -> int:
        return sum(run.jobs_new for run in self.scrape_runs)


class PipelineService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        preferences: UserPreferences,
        *,
        llm: LLMProvider | None,
        model_name: str = "",
    ) -> None:
        self._factory = session_factory
        self._prefs = preferences
        self._llm = llm
        self._model_name = model_name

    async def run(
        self,
        sources: list[JobSource],
        *,
        top: int = 5,
        min_score: int | None = None,
        match_limit: int = 100,
    ) -> PipelineReport:
        """Run every stage. Caller must hold the pipeline lock."""
        report = PipelineReport()

        runner = ScrapeRunner(self._factory, self._prefs)
        for source in sources:
            report.scrape_runs.append(await runner.run(source))  # never raises per source

        if self._llm is None:
            report.stopped_early = "LLM unavailable — skipped match, rank, and prepare"
            return report
        async with self._factory() as session:
            resume = await ResumeRepository(session).get_active()
        if resume is None or resume.id is None:
            report.stopped_early = "No active resume — skipped match, rank, and prepare"
            return report

        matcher = MatchService(
            self._factory, MatchEngine(self._llm, model_name=self._model_name), self._prefs
        )
        try:
            report.scored = len(await matcher.match_unscored(limit=match_limit))
            report.score_failures = len(matcher.last_failures)
        except MatchingAbortedError as exc:
            report.scored = len(exc.scored)
            report.stopped_early = f"Matching stopped: {exc}"
            return report

        # The configured threshold is a floor; --min-score can only raise it.
        threshold = max(self._prefs.min_match_score, min_score or 0)
        report.candidates = await matcher.ranked(
            min_score=threshold, top=max(top, 0) * 3 + 10, exclude_applied=True
        )

        service = ApplicationService(self._factory, self._prefs, llm=self._llm)
        for entry in report.candidates:
            if len(report.prepared) >= top:
                break
            assert entry.job.id is not None
            label = f"{entry.job.title} @ {entry.job.company}"
            try:
                application = await service.prepare(entry.job.id, resume_id=resume.id)
            except (DuplicateApplicationError, BlacklistedCompanyError) as exc:
                report.skipped.append(f"{label}: {exc}")
                continue
            except DailyCapReachedError as exc:
                report.stopped_early = str(exc)
                break
            assert application.id is not None
            report.prepared.append(application.id)
        return report
