from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.database.repositories import ScrapeRunRepository
from jobpilot.domain import JobSource, ScrapeRunStatus


class TestScrapeRunRepository:
    async def test_start_and_finish_success(self, session: AsyncSession) -> None:
        repo = ScrapeRunRepository(session)
        run = await repo.start(JobSource.REMOTEOK)
        assert run.id is not None
        assert run.status is ScrapeRunStatus.RUNNING

        finished = await repo.finish(run.id, jobs_found=42, jobs_new=7)
        assert finished is not None
        assert finished.status is ScrapeRunStatus.COMPLETED
        assert finished.jobs_new == 7
        assert finished.finished_at is not None

    async def test_finish_with_error_marks_failed(self, session: AsyncSession) -> None:
        repo = ScrapeRunRepository(session)
        run = await repo.start(JobSource.REMOTEOK)
        assert run.id is not None
        finished = await repo.finish(run.id, jobs_found=0, jobs_new=0, error="timeout")
        assert finished is not None
        assert finished.status is ScrapeRunStatus.FAILED
        assert finished.error == "timeout"

    async def test_list_recent_orders_newest_first(self, session: AsyncSession) -> None:
        repo = ScrapeRunRepository(session)
        await repo.start(JobSource.REMOTEOK)
        await repo.start(JobSource.GREENHOUSE)
        recent = await repo.list_recent()
        assert len(recent) == 2
