"""Job listing and search."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from jobpilot.api.deps import SessionDep
from jobpilot.database.repositories import JobRepository
from jobpilot.domain.enums import JobSource, RemoteType
from jobpilot.domain.models import Job

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("", response_model=list[Job])
async def list_jobs(
    session: SessionDep,
    source: JobSource | None = None,
    remote: RemoteType | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Job]:
    return await JobRepository(session).list(
        source=source, remote=remote, search=search, limit=min(limit, 200), offset=offset
    )


@router.get("/{job_id}", response_model=Job)
async def get_job(job_id: int, session: SessionDep) -> Job:
    job = await JobRepository(session).get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No job with id {job_id}")
    return job
