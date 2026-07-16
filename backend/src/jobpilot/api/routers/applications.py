"""Application listing."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from jobpilot.api.deps import SessionDep
from jobpilot.database.repositories import ApplicationRepository, JobRepository
from jobpilot.domain.enums import ApplicationStatus
from jobpilot.domain.models import Application, Job

router = APIRouter(prefix="/api/applications", tags=["applications"])


class ApplicationWithJob(BaseModel):
    application: Application
    job: Job | None


@router.get("", response_model=list[ApplicationWithJob])
async def list_applications(
    session: SessionDep,
    status: ApplicationStatus | None = None,
    limit: int = 100,
) -> list[ApplicationWithJob]:
    apps = await ApplicationRepository(session).list(status=status, limit=min(limit, 200))
    jobs_repo = JobRepository(session)
    result = []
    for app in apps:
        job = await jobs_repo.get(app.job_id)
        result.append(ApplicationWithJob(application=app, job=job))
    return result


@router.get("/{application_id}", response_model=ApplicationWithJob)
async def get_application(application_id: int, session: SessionDep) -> ApplicationWithJob:
    app = await ApplicationRepository(session).get(application_id)
    if app is None:
        raise HTTPException(status_code=404, detail=f"No application {application_id}")
    job = await JobRepository(session).get(app.job_id)
    return ApplicationWithJob(application=app, job=job)
