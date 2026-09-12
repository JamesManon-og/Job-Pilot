"""Scrape run bookkeeping — powers the dashboard's scraper status panel."""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.database.orm import ScrapeRunRow
from jobpilot.domain.enums import JobSource, ScrapeRunStatus
from jobpilot.domain.models import ScrapeRun, utcnow


def _to_domain(row: ScrapeRunRow) -> ScrapeRun:
    return ScrapeRun(
        id=row.id,
        source=JobSource(row.source),
        status=ScrapeRunStatus(row.status),
        jobs_found=row.jobs_found,
        jobs_new=row.jobs_new,
        error=row.error,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


class ScrapeRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def start(self, source: JobSource) -> ScrapeRun:
        row = ScrapeRunRow(
            source=source.value,
            status=ScrapeRunStatus.RUNNING.value,
            started_at=utcnow(),
        )
        self._session.add(row)
        await self._session.flush()
        return _to_domain(row)

    async def finish(
        self,
        run_id: int,
        *,
        jobs_found: int,
        jobs_new: int,
        error: str | None = None,
    ) -> ScrapeRun | None:
        row = await self._session.get(ScrapeRunRow, run_id)
        if row is None:
            return None
        row.jobs_found = jobs_found
        row.jobs_new = jobs_new
        row.error = error
        row.status = ScrapeRunStatus.FAILED.value if error else ScrapeRunStatus.COMPLETED.value
        row.finished_at = utcnow()
        await self._session.flush()
        return _to_domain(row)

    async def mark_interrupted(self, source: JobSource) -> int:
        """Close RUNNING rows left behind by a crashed or killed process.

        Only call this while holding the pipeline lock: at that point no other
        scrape can be running, so every RUNNING row for the source is stale.
        """
        result = await self._session.execute(
            update(ScrapeRunRow)
            .where(
                ScrapeRunRow.source == source.value,
                ScrapeRunRow.status == ScrapeRunStatus.RUNNING.value,
            )
            .values(
                status=ScrapeRunStatus.FAILED.value,
                error="interrupted: the process exited before the scrape finished",
                finished_at=utcnow(),
            )
        )
        await self._session.flush()
        return int(getattr(result, "rowcount", 0) or 0)

    async def list_recent(self, limit: int = 20) -> list[ScrapeRun]:
        rows = await self._session.scalars(
            select(ScrapeRunRow).order_by(ScrapeRunRow.started_at.desc()).limit(limit)
        )
        return [_to_domain(row) for row in rows]
