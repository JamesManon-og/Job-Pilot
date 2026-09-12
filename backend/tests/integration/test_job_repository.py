from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.database.repositories import JobRepository
from jobpilot.domain import Job, JobSource, JobStatus, RemoteType


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
        """Scheme/host case is folded into the same identity. Path case is kept
        (paths are case-sensitive; some boards use case-sensitive ids), but the
        variant still can't be processed twice: it's a fingerprint duplicate."""
        repo = JobRepository(session)
        original, _ = await repo.upsert(make_job())
        same_host_variant = make_job(url="HTTPS://A.COM/j/1")
        _, created = await repo.upsert(same_host_variant)
        assert created is False

        path_variant = Job(
            title="frontend engineer",
            company="ACME",
            application_url="https://a.com/J/1",
            source=JobSource.REMOTEOK,
        )
        dup, created = await repo.upsert(path_variant)
        assert created is True
        assert dup.status is JobStatus.SKIPPED
        assert dup.duplicate_of_id == original.id

    async def test_cross_platform_duplicate_is_skipped_not_reprocessed(
        self, session: AsyncSession
    ) -> None:
        repo = JobRepository(session)
        first, _ = await repo.upsert(make_job("Senior React Developer"))
        elsewhere = Job(
            title="Sr. React Developer (Remote)",
            company="Acme Inc.",
            application_url="https://www.jobstreet.com.ph/job/555",
            external_id="555",
            source=JobSource.JOBSTREET,
        )
        dup, created = await repo.upsert(elsewhere)
        assert created and dup.status is JobStatus.SKIPPED and dup.duplicate_of_id == first.id
        assert "duplicate of" in dup.status_reason
        # Re-scraping the duplicate keeps it skipped, and doesn't touch the original.
        again, created = await repo.upsert(elsewhere)
        assert not created and again.status is JobStatus.SKIPPED
        assert (await repo.get(first.id)).status is JobStatus.DISCOVERED  # type: ignore[arg-type,union-attr]

    async def test_rescrape_never_resets_pipeline_status(self, session: AsyncSession) -> None:
        repo = JobRepository(session)
        job, _ = await repo.upsert(make_job())
        assert job.id is not None
        await repo.set_status(job.id, JobStatus.APPLIED)
        refreshed, _ = await repo.upsert(make_job())
        assert refreshed.status is JobStatus.APPLIED

    async def test_external_id_identity_and_legacy_rows(self, session: AsyncSession) -> None:
        """A legacy url-only row is adopted by the first id-carrying scrape."""
        repo = JobRepository(session)
        legacy, _ = await repo.upsert(make_job(url="https://a.com/j/9?utm_source=x"))
        with_id = make_job(url="https://a.com/j/9")
        with_id.external_id = "9"
        with_id = Job.model_validate({**with_id.model_dump(), "dedup_hash": ""})
        adopted, created = await repo.upsert(with_id)
        assert not created and adopted.id == legacy.id and adopted.external_id == "9"
        # Later listing without the id still resolves to the same row.
        again, created = await repo.upsert(make_job(url="https://a.com/j/9"))
        assert not created and again.id == legacy.id and again.external_id == "9"
        assert await repo.count() == 1

    async def test_listing_refresh_keeps_enriched_description(self, session: AsyncSession) -> None:
        repo = JobRepository(session)
        detailed = make_job()
        detailed.description = "Full details from the job page"
        await repo.upsert(detailed)
        refreshed, _ = await repo.upsert(make_job())  # listing only, no description
        assert refreshed.description == "Full details from the job page"

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
