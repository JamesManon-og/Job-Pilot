"""`jobpilot run` pipeline under hostile conditions (audit: "try to break the full pipeline")."""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any, ClassVar

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from jobpilot.config.preferences import ApplicantProfile, UserPreferences
from jobpilot.database import create_session_factory
from jobpilot.database.repositories import (
    ApplicationRepository,
    JobRepository,
    MatchResultRepository,
    ResumeRepository,
)
from jobpilot.domain import JobSource, Resume, ScrapeRunStatus
from jobpilot.llm import OllamaUnavailableError
from jobpilot.pipeline import PipelineService
from jobpilot.platforms import SearchRunner
from tests.fakes import FakeAdapter, make_job

PREFS = dict(
    applicant=ApplicantProfile(name="James Manon", email="j@example.com"),
    remote_only=False,
    min_match_score=50,
)


class Feed(FakeAdapter):
    """The pipeline's job source for these tests (an API-style platform)."""


class BrokenAdapter(FakeAdapter):
    platform: ClassVar[JobSource] = JobSource.LEVER


FEED = Feed()


@pytest.fixture(autouse=True)
def reset_feed() -> Iterator[None]:
    FEED.jobs = []
    FEED.enriched = []
    yield


class ScoringLLM:
    """Scores by job number: job i scores `scores[i]` (default 80)."""

    def __init__(self, scores: dict[int, int] | None = None, die_after: int | None = None) -> None:
        self.scores = scores or {}
        self.die_after = die_after
        self.calls = 0

    async def generate(self, prompt: str, *, system: str | None = None) -> str:
        return "Cover letter"

    async def generate_json(
        self, prompt: str, *, system: str | None = None, schema: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if schema and "why_company" in schema.get("properties", {}):
            return {"why_company": "Mission.", "why_qualified": "Skills."}
        self.calls += 1
        if self.die_after is not None and self.calls > self.die_after:
            raise OllamaUnavailableError("Cannot reach Ollama")
        number = int(prompt.split("Title: Engineer ")[1].split()[0])
        return {"score": self.scores.get(number, 80), "recommendation": "apply"}


async def with_resume(engine: AsyncEngine) -> None:
    async with create_session_factory(engine)() as session:
        await ResumeRepository(session).add(
            Resume(version="v1", file_path="/r.pdf", is_active=True)
        )
        await session.commit()


def pipeline(engine: AsyncEngine, llm: object | None, **prefs: Any) -> PipelineService:
    factory = create_session_factory(engine)
    preferences = UserPreferences(**{**PREFS, **prefs})
    search = SearchRunner(
        factory,
        preferences,
        adapters={
            JobSource.OTHER: FEED,
            JobSource.LEVER: BrokenAdapter(raise_in_search=RuntimeError("HTTP 503 from lever")),
        },
    )
    return PipelineService(factory, preferences, llm=llm, search=search)  # type: ignore[arg-type]


class TestPipeline:
    async def test_zero_jobs(self, engine: AsyncEngine) -> None:
        await with_resume(engine)
        report = await pipeline(engine, ScoringLLM()).run([JobSource.OTHER])
        assert report.new_jobs == 0 and report.scored == 0 and report.prepared == []
        assert report.stopped_early is None

    async def test_ollama_offline_still_scrapes(self, engine: AsyncEngine) -> None:
        FEED.jobs = [make_job(1)]
        report = await pipeline(engine, None).run([JobSource.OTHER])
        assert report.new_jobs == 1
        assert report.stopped_early and "LLM unavailable" in report.stopped_early

    async def test_no_resume(self, engine: AsyncEngine) -> None:
        FEED.jobs = [make_job(1)]
        report = await pipeline(engine, ScoringLLM()).run([JobSource.OTHER])
        assert report.stopped_early and "No active resume" in report.stopped_early

    async def test_one_failing_source_does_not_stop_the_others(self, engine: AsyncEngine) -> None:
        await with_resume(engine)
        FEED.jobs = [make_job(1)]
        report = await pipeline(engine, ScoringLLM()).run([JobSource.LEVER, JobSource.OTHER])
        statuses = {run.source: run.status for run in report.scrape_runs}
        assert statuses == {
            JobSource.LEVER: ScrapeRunStatus.FAILED,
            JobSource.OTHER: ScrapeRunStatus.COMPLETED,
        }
        assert len(report.prepared) == 1

    async def test_min_match_score_from_config_is_enforced(self, engine: AsyncEngine) -> None:
        """Bug: min_match_score was never read; `run` prepared the top N no
        matter how badly they scored."""
        await with_resume(engine)
        FEED.jobs = [make_job(1), make_job(2)]
        llm = ScoringLLM({1: 30, 2: 74})
        report = await pipeline(engine, llm, min_match_score=75).run([JobSource.OTHER], top=5)
        assert report.scored == 2
        assert report.prepared == []

    async def test_cli_min_score_can_only_raise_the_threshold(self, engine: AsyncEngine) -> None:
        await with_resume(engine)
        FEED.jobs = [make_job(1)]
        report = await pipeline(engine, ScoringLLM({1: 60}), min_match_score=50).run(
            [JobSource.OTHER], min_score=0
        )
        assert len(report.prepared) == 1  # 0 from CLI doesn't lower 50 from config

    async def test_rerun_moves_on_to_new_jobs_instead_of_stalling(
        self, engine: AsyncEngine
    ) -> None:
        """Bug: ranked() included already-applied jobs, so run #2 re-tried the same
        top N, hit "already applied" for each, and prepared nothing — forever."""
        await with_resume(engine)
        FEED.jobs = [make_job(i) for i in range(1, 5)]
        llm = ScoringLLM({1: 95, 2: 90, 3: 85, 4: 80})
        first = await pipeline(engine, llm).run([JobSource.OTHER], top=2)
        second = await pipeline(engine, llm).run([JobSource.OTHER], top=2)
        third = await pipeline(engine, llm).run([JobSource.OTHER], top=2)

        assert len(first.prepared) == 2
        assert len(second.prepared) == 2
        assert third.prepared == []
        async with create_session_factory(engine)() as session:
            assert len(await ApplicationRepository(session).job_ids_with_applications()) == 4

    async def test_rerunning_creates_no_duplicates(self, engine: AsyncEngine) -> None:
        await with_resume(engine)
        FEED.jobs = [make_job(1), make_job(1), make_job(2)]  # duplicate listing
        for _ in range(3):
            await pipeline(engine, ScoringLLM()).run([JobSource.OTHER], top=5)
        async with create_session_factory(engine)() as session:
            assert await JobRepository(session).count() == 2
            assert await MatchResultRepository(session).count_for_resume(1) == 2
            assert len(await ApplicationRepository(session).list()) == 2

    async def test_ollama_crash_mid_matching_keeps_progress(self, engine: AsyncEngine) -> None:
        await with_resume(engine)
        FEED.jobs = [make_job(i) for i in range(1, 6)]
        report = await pipeline(engine, ScoringLLM(die_after=2)).run([JobSource.OTHER])
        assert report.scored == 2
        assert report.stopped_early and "Matching stopped" in report.stopped_early
        assert report.prepared == []
        # Next run (Ollama back) scores only the remaining three.
        llm = ScoringLLM()
        await pipeline(engine, llm).run([JobSource.OTHER], top=0)
        assert llm.calls == 3

    async def test_daily_cap_stops_preparation(self, engine: AsyncEngine) -> None:
        await with_resume(engine)
        FEED.jobs = [make_job(i) for i in range(1, 4)]
        svc = pipeline(engine, ScoringLLM(), max_applications_per_day=1)
        first = await svc.run([JobSource.OTHER], top=1)
        app_id = first.prepared[0]
        from jobpilot.applications import ApplicationService

        service = ApplicationService(
            create_session_factory(engine),
            UserPreferences(**{**PREFS, "max_applications_per_day": 1}),
        )
        await service.approve(app_id)
        await service.claim_for_autofill(app_id)
        await service.mark_submitted(app_id)
        second = await svc.run([JobSource.OTHER], top=3)
        assert second.prepared == []
        assert second.stopped_early and "Daily cap" in second.stopped_early

    async def test_hundreds_of_jobs(self, engine: AsyncEngine) -> None:
        await with_resume(engine)
        FEED.jobs = [make_job(i) for i in range(1, 301)]
        started = time.perf_counter()
        report = await pipeline(engine, ScoringLLM({i: i % 100 for i in range(1, 301)})).run(
            [JobSource.OTHER], top=5, match_limit=300
        )
        assert report.new_jobs == 300 and report.scored == 300
        assert len(report.prepared) == 5
        assert all(entry.match.score >= 50 for entry in report.candidates)
        assert time.perf_counter() - started < 30
