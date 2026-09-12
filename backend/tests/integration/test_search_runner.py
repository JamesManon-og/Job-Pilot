"""SearchRunner: per-platform isolation, two-phase dedup, sessions, filters, resilience.

Includes the scrape-runner guarantees from the reliability audit (one bad
listing never kills a run; interrupted runs are closed) now that discovery
goes through platform adapters.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from jobpilot.config.preferences import PlatformSettings, SearchSettings, UserPreferences
from jobpilot.database import create_session_factory
from jobpilot.database.repositories import (
    JobRepository,
    PlatformSessionRepository,
    ScrapeRunRepository,
)
from jobpilot.domain import EmploymentType, JobSource, JobStatus, RemoteType, ScrapeRunStatus
from jobpilot.domain.enums import SessionStatus
from jobpilot.platforms import ChallengeError, SearchRunner, SessionExpiredError
from tests.fakes import FakeAdapter, FakeBrowserAdapter, FakeProfiles, make_job


def prefs(**kwargs: Any) -> UserPreferences:
    base: dict[str, Any] = {"remote_only": False, "search": SearchSettings(queries=["engineer"])}
    base.update(kwargs)
    return UserPreferences(**base)


def runner(engine: AsyncEngine, *adapters: FakeAdapter, **kwargs: Any) -> SearchRunner:
    return SearchRunner(
        create_session_factory(engine),
        kwargs.pop("preferences", prefs()),
        adapters={a.platform: a for a in adapters},
        profiles=kwargs.pop("profiles", FakeProfiles()),  # type: ignore[arg-type]
        **kwargs,
    )


async def job_count(engine: AsyncEngine) -> int:
    async with create_session_factory(engine)() as session:
        return await JobRepository(session).count()


class TestDiscovery:
    async def test_persists_new_jobs_and_records_the_run(self, engine: AsyncEngine) -> None:
        adapter = FakeAdapter([make_job(1), make_job(2)])
        run = await runner(engine, adapter).run(JobSource.OTHER)
        assert run.status is ScrapeRunStatus.COMPLETED
        assert (run.jobs_found, run.jobs_new) == (2, 2)
        assert await job_count(engine) == 2

    async def test_known_jobs_are_not_re_enriched(self, engine: AsyncEngine) -> None:
        """ "Detect whether a job has already been processed" from the results page
        alone: no detail-page load for postings we already have."""
        adapter = FakeAdapter([make_job(1), make_job(2)])
        await runner(engine, adapter).run(JobSource.OTHER)
        assert adapter.enriched == ["1", "2"]

        adapter.jobs.append(make_job(3))
        search = runner(engine, adapter)
        run = await search.run(JobSource.OTHER)
        assert adapter.enriched == ["1", "2", "3"]  # only the new one
        assert run.jobs_new == 1
        assert search.last_stats[JobSource.OTHER].known == 2

    async def test_same_listing_under_two_queries_is_one_job(self, engine: AsyncEngine) -> None:
        adapter = FakeAdapter([make_job(1, title="React Engineer")])
        preferences = prefs(search=SearchSettings(queries=["react", "engineer"]))
        run = await runner(engine, adapter, preferences=preferences).run(JobSource.OTHER)
        assert (run.jobs_found, run.jobs_new) == (2, 1)
        assert adapter.enriched == ["1"]

    async def test_cross_platform_duplicate_is_skipped(self, engine: AsyncEngine) -> None:
        other = FakeAdapter([make_job(1, title="Senior React Developer")])
        same_job = make_job(1, title="Sr. React Developer (Remote)", source=JobSource.LINKEDIN)
        # Adapters may edit fields with model_copy (no validators): identity must follow.
        same_job = same_job.model_copy(update={"company": "Acme1 Inc."})
        linkedin = FakeBrowserAdapter([same_job])
        preferences = prefs(search=SearchSettings(queries=["react"]))
        search = runner(engine, other, linkedin, preferences=preferences)
        await search.run(JobSource.OTHER)
        await search.run(JobSource.LINKEDIN)
        async with create_session_factory(engine)() as session:
            jobs = await JobRepository(session).list()
        by_source = {j.source: j for j in jobs}
        assert by_source[JobSource.LINKEDIN].status is JobStatus.SKIPPED
        assert by_source[JobSource.LINKEDIN].duplicate_of_id == by_source[JobSource.OTHER].id

    async def test_api_platform_without_queries_keeps_everything(self, engine: AsyncEngine) -> None:
        adapter = FakeAdapter([make_job(1, title="Designer"), make_job(2)])
        run = await runner(engine, adapter, preferences=UserPreferences(remote_only=False)).run(
            JobSource.OTHER
        )
        assert run.jobs_new == 2

    async def test_browser_platform_without_queries_is_a_clear_error(
        self, engine: AsyncEngine
    ) -> None:
        adapter = FakeBrowserAdapter([make_job(1)])
        run = await runner(engine, adapter, preferences=UserPreferences()).run(JobSource.LINKEDIN)
        assert run.status is ScrapeRunStatus.FAILED
        assert run.error is not None and "search.queries" in run.error


class TestFilters:
    async def test_blacklist_exclusions_remote_and_employment_type(
        self, engine: AsyncEngine
    ) -> None:
        adapter = FakeAdapter(
            [
                make_job(1),
                make_job(2, company="SpamCorp"),
                make_job(3, title="Staff Engineer"),
                make_job(4, remote=RemoteType.ONSITE),
                make_job(5, employment_type=EmploymentType.INTERNSHIP),
                make_job(6, description="Must have 10+ years of COBOL"),
            ]
        )
        preferences = prefs(
            remote_only=True,
            blacklist_companies=["SpamCorp2"],
            excluded_keywords=["staff", "cobol"],
            employment_types=["full_time", "contract"],
        )
        search = runner(engine, adapter, preferences=preferences)
        run = await search.run(JobSource.OTHER)
        assert run.jobs_new == 1
        assert search.last_stats[JobSource.OTHER].filtered == 5
        # Title-level exclusions are decided from the listing, before any detail load.
        assert "2" not in adapter.enriched and "3" not in adapter.enriched


class TestResilience:
    async def test_database_error_on_one_listing_does_not_kill_the_run(
        self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Audit bug: one IntegrityError poisoned the session and the run stayed
        RUNNING forever. Now the listing is skipped and the rest are saved."""
        from jobpilot.database.orm import JobRow
        from jobpilot.database.repositories.jobs import _apply_to_row

        original = JobRepository.upsert

        async def flaky_upsert(self: JobRepository, job: Any) -> Any:
            if job.external_id == "2":
                row = JobRow()
                _apply_to_row(make_job(99), row)
                row.dedup_hash = make_job(1).dedup_hash  # real flush-time IntegrityError
                row.status = "discovered"
                self._session.add(row)
                await self._session.flush()
            return await original(self, job)

        monkeypatch.setattr(JobRepository, "upsert", flaky_upsert)
        adapter = FakeAdapter([make_job(1), make_job(2), make_job(3)])
        search = runner(engine, adapter)
        run = await search.run(JobSource.OTHER)
        assert run.status is ScrapeRunStatus.COMPLETED
        assert (run.jobs_found, run.jobs_new) == (3, 2)
        assert search.last_stats[JobSource.OTHER].failed == 1
        assert await job_count(engine) == 2

    async def test_interrupted_runs_are_closed_on_next_start(self, engine: AsyncEngine) -> None:
        """Audit bug: 6 scrape_runs rows in the real DB were stuck in RUNNING."""
        factory = create_session_factory(engine)
        async with factory() as session:
            stale = await ScrapeRunRepository(session).start(JobSource.OTHER)
            await session.commit()
        await runner(engine, FakeAdapter([make_job(1)])).run(JobSource.OTHER)
        async with factory() as session:
            runs = {r.id: r for r in await ScrapeRunRepository(session).list_recent()}
        assert runs[stale.id].status is ScrapeRunStatus.FAILED
        assert runs[stale.id].error and "interrupted" in runs[stale.id].error

    async def test_one_failing_query_does_not_stop_the_others(self, engine: AsyncEngine) -> None:
        adapter = FakeAdapter([make_job(1, title="React Engineer")], fail_query="vue")
        preferences = prefs(search=SearchSettings(queries=["vue", "react"]))
        run = await runner(engine, adapter, preferences=preferences).run(JobSource.OTHER)
        assert run.jobs_new == 1
        assert run.error is not None and "vue" in run.error

    async def test_detail_page_failures_trip_the_circuit_breaker(self, engine: AsyncEngine) -> None:
        adapter = FakeAdapter([make_job(i) for i in range(1, 11)], fail_enrich=lambda j: True)
        run = await runner(engine, adapter).run(JobSource.OTHER)
        assert run.status is ScrapeRunStatus.FAILED
        assert run.error is not None and "in a row" in run.error
        assert len(adapter.enriched) == 5

    async def test_occasional_detail_failure_is_skipped(self, engine: AsyncEngine) -> None:
        adapter = FakeAdapter(
            [make_job(i) for i in range(1, 4)], fail_enrich=lambda j: j.external_id == "2"
        )
        run = await runner(engine, adapter).run(JobSource.OTHER)
        assert run.status is ScrapeRunStatus.COMPLETED and run.jobs_new == 2

    async def test_pause_stops_at_the_next_listing(self, engine: AsyncEngine) -> None:
        adapter = FakeAdapter([make_job(i) for i in range(1, 6)])
        calls = {"n": 0}

        def paused() -> bool:
            calls["n"] += 1
            return calls["n"] > 3  # pause after a couple of listings

        run = await runner(engine, adapter, paused=paused).run(JobSource.OTHER)
        assert run.error == "paused"
        assert 0 < run.jobs_new < 5


class TestSessions:
    async def test_expired_session_marks_needs_login_and_stops_only_that_platform(
        self, engine: AsyncEngine
    ) -> None:
        linkedin = FakeBrowserAdapter(
            [make_job(1, source=JobSource.LINKEDIN)], session=SessionStatus.NEEDS_LOGIN
        )
        other = FakeAdapter([make_job(2)])
        search = runner(engine, linkedin, other)
        first = await search.run(JobSource.LINKEDIN)
        second = await search.run(JobSource.OTHER)
        assert first.status is ScrapeRunStatus.FAILED
        assert first.error is not None and "jobpilot login linkedin" in first.error
        assert linkedin.enriched == []
        assert second.status is ScrapeRunStatus.COMPLETED and second.jobs_new == 1
        async with create_session_factory(engine)() as session:
            saved = await PlatformSessionRepository(session).get(JobSource.LINKEDIN)
        assert saved is not None and saved.status is SessionStatus.NEEDS_LOGIN

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (SessionExpiredError(JobSource.LINKEDIN, "https://x/login"), SessionStatus.NEEDS_LOGIN),
            (ChallengeError(JobSource.LINKEDIN, "challenge"), SessionStatus.BLOCKED),
        ],
    )
    async def test_walls_mid_search_are_recorded_not_bypassed(
        self, engine: AsyncEngine, error: Exception, expected: SessionStatus
    ) -> None:
        linkedin = FakeBrowserAdapter([], raise_in_search=error)
        run = await runner(engine, linkedin).run(JobSource.LINKEDIN)
        assert run.status is ScrapeRunStatus.FAILED
        async with create_session_factory(engine)() as session:
            saved = await PlatformSessionRepository(session).get(JobSource.LINKEDIN)
        assert saved is not None and saved.status is expected

    async def test_logged_in_browser_platform_searches(self, engine: AsyncEngine) -> None:
        profiles = FakeProfiles()
        linkedin = FakeBrowserAdapter([make_job(1, source=JobSource.LINKEDIN)])
        run = await runner(engine, linkedin, profiles=profiles).run(JobSource.LINKEDIN)
        assert run.jobs_new == 1
        assert profiles.opened == [JobSource.LINKEDIN]


class TestEnabledPlatforms:
    def test_remoteok_enabled_by_default_and_kept_when_others_configured(self) -> None:
        from jobpilot.platforms import enabled_platforms

        assert enabled_platforms(UserPreferences()) == [JobSource.REMOTEOK]
        configured = UserPreferences.model_validate({"platforms": {"linkedin": {"enabled": True}}})
        assert configured.platform("remoteok").enabled
        assert configured.platform("linkedin") == PlatformSettings(enabled=True)
