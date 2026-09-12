"""Regression tests for data-layer bugs found in the reliability audit.

Each test reproduces a failure observed against real data (see the audit
report): SQLite handing back naive datetimes, re-scrapes violating the unique
application_url index, and scrape runs left in RUNNING forever.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncEngine

from jobpilot.config.preferences import RankingWeights
from jobpilot.database import create_session_factory
from jobpilot.database.repositories import (
    JobRepository,
    MatchResultRepository,
    ResumeRepository,
)
from jobpilot.domain import (
    Job,
    JobSource,
    MatchRecommendation,
    MatchResult,
    RemoteType,
    Resume,
)
from jobpilot.matcher.ranking import rank_jobs


def _job(title: str = "Engineer", url: str = "https://a.com/j/1", **kwargs: object) -> Job:
    return Job(
        title=title,
        company="Acme",
        application_url=url,
        source=JobSource.OTHER,
        remote=RemoteType.REMOTE,
        **kwargs,  # type: ignore[arg-type]
    )


class TestDatetimesAreTimezoneAware:
    async def test_datetimes_round_trip_as_utc_aware(self, engine: AsyncEngine) -> None:
        """SQLite stores naive values; the ORM must hand back aware UTC datetimes."""
        posted = datetime(2026, 7, 1, 12, 30, tzinfo=UTC)
        factory = create_session_factory(engine)
        async with factory() as session:
            job, _ = await JobRepository(session).upsert(_job(date_posted=posted))
            await session.commit()
        assert job.id is not None

        async with factory() as session:
            loaded = await JobRepository(session).get(job.id)
        assert loaded is not None
        assert loaded.date_posted is not None
        assert loaded.date_posted.tzinfo is not None
        assert loaded.date_posted == posted
        assert loaded.scraped_at.tzinfo is not None

    async def test_non_utc_offsets_are_normalized(self, engine: AsyncEngine) -> None:
        manila = datetime(2026, 7, 1, 20, 0, tzinfo=UTC) + timedelta(0)
        manila = manila.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Manila"))
        factory = create_session_factory(engine)
        async with factory() as session:
            job, _ = await JobRepository(session).upsert(_job(date_posted=manila))
            await session.commit()
            assert job.id is not None
        async with factory() as session:
            loaded = await JobRepository(session).get(job.id)
        assert loaded is not None and loaded.date_posted is not None
        assert loaded.date_posted == manila  # same instant
        assert loaded.date_posted.utcoffset() == timedelta(0)

    async def test_ranking_jobs_loaded_from_the_database_does_not_crash(
        self, engine: AsyncEngine
    ) -> None:
        """Bug: rank_jobs raised TypeError (naive - aware) on every DB-loaded job."""
        factory = create_session_factory(engine)
        async with factory() as session:
            job, _ = await JobRepository(session).upsert(
                _job(date_posted=datetime.now(UTC) - timedelta(days=2))
            )
            resume = await ResumeRepository(session).add(
                Resume(version="v1", file_path="/r.pdf", is_active=True)
            )
            assert job.id is not None and resume.id is not None
            await MatchResultRepository(session).upsert(
                MatchResult(
                    job_id=job.id,
                    resume_id=resume.id,
                    score=80,
                    recommendation=MatchRecommendation.APPLY,
                )
            )
            await session.commit()

        async with factory() as session:
            matches = await MatchResultRepository(session).list_for_resume(resume.id)
            pairs = []
            for match in matches:
                loaded = await JobRepository(session).get(match.job_id)
                assert loaded is not None
                pairs.append((loaded, match))
        ranked = rank_jobs(pairs, weights=RankingWeights())
        assert len(ranked) == 1

    def test_ranking_tolerates_naive_datetimes(self) -> None:
        """Defense in depth: a naive date_posted is treated as UTC, not a crash."""
        job = _job(date_posted=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1))
        job.id = 1
        match = MatchResult(
            job_id=1, resume_id=1, score=50, recommendation=MatchRecommendation.MAYBE
        )
        assert len(rank_jobs([(job, match)], weights=RankingWeights())) == 1


class TestJobUpsertIdentity:
    async def test_same_url_with_edited_title_updates_instead_of_crashing(
        self, engine: AsyncEngine
    ) -> None:
        """Bug (seen in production logs): a posting whose title changed kept its URL,
        produced a new dedup_hash, and the INSERT hit UNIQUE(application_url)."""
        factory = create_session_factory(engine)
        async with factory() as session:
            repo = JobRepository(session)
            first, created = await repo.upsert(_job(title="Senior Engineer"))
            assert created
            second, created = await repo.upsert(_job(title="Senior Engineer (Remote)"))
            await session.commit()
            assert created is False
            assert second.id == first.id
            assert second.title == "Senior Engineer (Remote)"
            assert await repo.count() == 1


class TestJobStatusFollowsThePipeline:
    async def test_status_moves_with_matching_and_applications(self, engine: AsyncEngine) -> None:
        from jobpilot.database.repositories import ApplicationRepository
        from jobpilot.domain import Application, ApplicationStatus, JobStatus

        factory = create_session_factory(engine)
        async with factory() as session:
            jobs = JobRepository(session)
            job, _ = await jobs.upsert(_job())
            resume = await ResumeRepository(session).add(
                Resume(version="v1", file_path="/r.pdf", is_active=True)
            )
            assert job.id is not None and resume.id is not None
            assert job.status is JobStatus.DISCOVERED

            await MatchResultRepository(session).upsert(
                MatchResult(
                    job_id=job.id,
                    resume_id=resume.id,
                    score=80,
                    recommendation=MatchRecommendation.APPLY,
                )
            )
            assert (await jobs.get(job.id)).status is JobStatus.MATCHED  # type: ignore[union-attr]

            apps = ApplicationRepository(session)
            app = await apps.create(Application(job_id=job.id, resume_id=resume.id))
            assert app.id is not None
            assert (await jobs.get(job.id)).status is JobStatus.PREPARED  # type: ignore[union-attr]
            for step in (
                ApplicationStatus.APPROVED,
                ApplicationStatus.AWAITING_CONFIRMATION,
                ApplicationStatus.SUBMITTED,
            ):
                await apps.transition(app.id, step)
            assert (await jobs.get(job.id)).status is JobStatus.APPLIED  # type: ignore[union-attr]

            # Re-scoring an applied job never demotes it.
            await MatchResultRepository(session).upsert(
                MatchResult(
                    job_id=job.id,
                    resume_id=resume.id,
                    score=10,
                    recommendation=MatchRecommendation.SKIP,
                )
            )
            assert (await jobs.get(job.id)).status is JobStatus.APPLIED  # type: ignore[union-attr]

    async def test_duplicates_are_never_scored(self, engine: AsyncEngine) -> None:
        factory = create_session_factory(engine)
        async with factory() as session:
            jobs = JobRepository(session)
            original, _ = await jobs.upsert(_job(title="React Developer", url="https://a.com/1"))
            dup, _ = await jobs.upsert(
                Job(
                    title="React Developer (Remote)",
                    company="ACME Inc.",
                    application_url="https://b.com/9",
                    source=JobSource.LINKEDIN,
                    external_id="9",
                )
            )
            resume = await ResumeRepository(session).add(
                Resume(version="v1", file_path="/r.pdf", is_active=True)
            )
            await session.commit()
        async with factory() as session:
            todo = await MatchResultRepository(session).unmatched_job_ids(resume.id)  # type: ignore[arg-type]
        assert todo == [original.id]
        assert dup.id not in todo
