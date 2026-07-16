from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.database.repositories import JobRepository
from jobpilot.domain import Job, JobSource, RemoteType


def make_job(title: str = "Frontend Engineer", url: str = "https://a.com/j/1") -> Job:
    return Job(
        title=title,
        company="Acme",
        application_url=url,
        source=JobSource.REMOTEOK,
        remote=RemoteType.REMOTE,
        technologies=["react", "typescript"],
    )


class TestJobRepository:
    async def test_upsert_inserts_new_job(self, session: AsyncSession) -> None:
        repo = JobRepository(session)
        job, created = await repo.upsert(make_job())
        assert created is True
        assert job.id is not None
        assert await repo.count() == 1

    async def test_upsert_same_job_twice_does_not_duplicate(self, session: AsyncSession) -> None:
        repo = JobRepository(session)
        first, _ = await repo.upsert(make_job())
        second, created = await repo.upsert(make_job())
        assert created is False
        assert second.id == first.id
        assert await repo.count() == 1

    async def test_upsert_refreshes_existing_row(self, session: AsyncSession) -> None:
        repo = JobRepository(session)
        await repo.upsert(make_job())
        updated = make_job()
        updated.description = "New description"
        job, created = await repo.upsert(updated)
        assert created is False
        assert job.description == "New description"

    async def test_case_variant_of_same_posting_is_deduped(self, session: AsyncSession) -> None:
        repo = JobRepository(session)
        await repo.upsert(make_job())
        variant = Job(
            title="frontend engineer",
            company="ACME",
            application_url="HTTPS://A.COM/J/1",
            source=JobSource.REMOTEOK,
        )
        _, created = await repo.upsert(variant)
        assert created is False
        assert await repo.count() == 1

    async def test_list_filters(self, session: AsyncSession) -> None:
        repo = JobRepository(session)
        await repo.upsert(make_job("Frontend Engineer", "https://a.com/j/1"))
        await repo.upsert(make_job("Data Scientist", "https://a.com/j/2"))

        results = await repo.list(search="frontend")
        assert [j.title for j in results] == ["Frontend Engineer"]

        results = await repo.list(source=JobSource.REMOTEOK)
        assert len(results) == 2

        results = await repo.list(source=JobSource.LINKEDIN)
        assert results == []

    async def test_json_fields_roundtrip(self, session: AsyncSession) -> None:
        repo = JobRepository(session)
        job, _ = await repo.upsert(make_job())
        fetched = await repo.get(job.id or 0)
        assert fetched is not None
        assert fetched.technologies == ["react", "typescript"]
