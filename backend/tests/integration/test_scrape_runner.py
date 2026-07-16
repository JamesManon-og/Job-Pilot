from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from jobpilot.config.preferences import UserPreferences
from jobpilot.database import create_session_factory
from jobpilot.database.repositories import JobRepository
from jobpilot.domain import Job, JobSource, RemoteType, ScrapeRunStatus
from jobpilot.scrapers.base import BaseScraper
from jobpilot.scrapers.registry import _REGISTRY
from jobpilot.scrapers.runner import ScrapeRunner


def make_jobs() -> list[Job]:
    return [
        Job(
            title="Remote Engineer",
            company="Acme",
            application_url="https://a.com/1",
            source=JobSource.OTHER,
            remote=RemoteType.REMOTE,
        ),
        Job(
            title="Onsite Engineer",
            company="Acme",
            application_url="https://a.com/2",
            source=JobSource.OTHER,
            remote=RemoteType.ONSITE,
        ),
        Job(
            title="Engineer",
            company="Blacklisted Inc",
            application_url="https://a.com/3",
            source=JobSource.OTHER,
            remote=RemoteType.REMOTE,
        ),
    ]


class FakeScraper(BaseScraper):
    source: ClassVar[JobSource] = JobSource.OTHER
    fail: ClassVar[bool] = False

    async def scrape(self) -> AsyncIterator[Job]:
        for job in make_jobs():
            yield job
        if self.fail:
            raise RuntimeError("boom")


@pytest.fixture(autouse=True)
def fake_registry() -> AsyncIterator[None]:  # type: ignore[misc]
    previous = _REGISTRY.pop(JobSource.OTHER, None)
    _REGISTRY[JobSource.OTHER] = FakeScraper
    FakeScraper.fail = False
    yield  # type: ignore[misc]
    _REGISTRY.pop(JobSource.OTHER, None)
    if previous is not None:
        _REGISTRY[JobSource.OTHER] = previous


class TestScrapeRunner:
    async def test_run_filters_and_persists(self, engine: AsyncEngine) -> None:
        factory = create_session_factory(engine)
        prefs = UserPreferences(remote_only=True, blacklist_companies=["Blacklisted Inc"])
        runner = ScrapeRunner(factory, prefs)

        run = await runner.run(JobSource.OTHER)
        assert run.status is ScrapeRunStatus.COMPLETED
        assert run.jobs_found == 3
        assert run.jobs_new == 1  # onsite filtered, blacklisted filtered

        async with factory() as session:
            assert await JobRepository(session).count() == 1

    async def test_rerun_finds_no_new_jobs(self, engine: AsyncEngine) -> None:
        factory = create_session_factory(engine)
        runner = ScrapeRunner(factory, UserPreferences(remote_only=False))

        first = await runner.run(JobSource.OTHER)
        assert first.jobs_new == 3
        second = await runner.run(JobSource.OTHER)
        assert second.jobs_new == 0
        async with factory() as session:
            assert await JobRepository(session).count() == 3

    async def test_failure_recorded_with_partial_results(self, engine: AsyncEngine) -> None:
        factory = create_session_factory(engine)
        runner = ScrapeRunner(factory, UserPreferences(remote_only=False))
        FakeScraper.fail = True

        run = await runner.run(JobSource.OTHER)
        assert run.status is ScrapeRunStatus.FAILED
        assert run.error is not None and "boom" in run.error
        assert run.jobs_new == 3  # jobs yielded before the crash are kept
