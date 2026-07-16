"""Human-approval review queue: list pending, approve, reject, edit materials."""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from jobpilot.api.deps import SessionDep, StateDep
from jobpilot.database.orm import ApplicationRow, JobRow
from jobpilot.domain.enums import ApplicationStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/review", tags=["review"])


class ReviewItem(BaseModel):
    application_id: int
    job_id: int
    title: str
    company: str
    application_url: str
    status: str
    cover_letter: str
    answers: dict[str, str]
    notes: str
    created_at: str


class EditMaterials(BaseModel):
    cover_letter: str | None = None
    answers: dict[str, str] | None = None


class ReviewAction(BaseModel):
    status: str
    detail: str = ""


@router.get("", response_model=list[ReviewItem])
async def review_queue(session: SessionDep, status: str = "pending_review") -> list[ReviewItem]:
    """List applications awaiting human review."""
    stmt = (
        select(ApplicationRow, JobRow)
        .join(JobRow, JobRow.id == ApplicationRow.job_id)
        .where(ApplicationRow.status == status)
        .order_by(ApplicationRow.created_at.desc())
    )
    rows = (await session.execute(stmt)).all()
    return [
        ReviewItem(
            application_id=app.id,
            job_id=job.id,
            title=job.title,
            company=job.company,
            application_url=job.application_url,
            status=app.status,
            cover_letter=app.cover_letter,
            answers=app.answers,
            notes=app.notes,
            created_at=app.created_at.isoformat() if app.created_at else "",
        )
        for app, job in rows
    ]


@router.patch("/{application_id}", response_model=ReviewAction)
async def edit_materials(
    application_id: int, body: EditMaterials, session: SessionDep
) -> ReviewAction:
    """Edit cover letter or answers before approving."""
    row = await session.get(ApplicationRow, application_id)
    if row is None:
        raise HTTPException(404, "Application not found")
    if row.status != ApplicationStatus.PENDING_REVIEW.value:
        raise HTTPException(409, f"Application is {row.status}, not pending_review")

    if body.cover_letter is not None:
        row.cover_letter = body.cover_letter
    if body.answers is not None:
        row.answers = body.answers
    await session.commit()
    return ReviewAction(status="updated", detail="Materials updated")


@router.post("/{application_id}/approve", response_model=ReviewAction)
async def approve(
    application_id: int,
    background: BackgroundTasks,
    session: SessionDep,
    state: StateDep,
) -> ReviewAction:
    """Approve an application and trigger submission in the background."""
    row = await session.get(ApplicationRow, application_id)
    if row is None:
        raise HTTPException(404, "Application not found")
    if row.status != ApplicationStatus.PENDING_REVIEW.value:
        raise HTTPException(409, f"Application is {row.status}, not pending_review")

    row.status = ApplicationStatus.APPROVED.value
    await session.commit()

    background.add_task(_submit_application, application_id, state)
    return ReviewAction(status="approved", detail="Submission started in background")


@router.post("/{application_id}/reject", response_model=ReviewAction)
async def reject(application_id: int, session: SessionDep) -> ReviewAction:
    """Reject an application — it won't be submitted."""
    row = await session.get(ApplicationRow, application_id)
    if row is None:
        raise HTTPException(404, "Application not found")
    if row.status not in (
        ApplicationStatus.PENDING_REVIEW.value,
        ApplicationStatus.APPROVED.value,
    ):
        raise HTTPException(409, f"Application is {row.status}, cannot reject")

    row.status = ApplicationStatus.REJECTED.value
    await session.commit()
    return ReviewAction(status="rejected", detail="Application rejected")


async def _submit_application(application_id: int, state: object) -> None:
    """Background task: run autofill + submit via ApplicationService."""
    from jobpilot.api.deps import AppState
    from jobpilot.applications import ApplicationService
    from jobpilot.autofill import AutofillEngine
    from jobpilot.llm import OllamaClient

    app_state: AppState = state  # type: ignore[assignment]

    prefs = app_state.preferences()
    llm: OllamaClient | None = None
    try:
        client = OllamaClient(app_state.settings.ollama_base_url, app_state.settings.ollama_model)
        if await client.is_available() and await client.has_model():
            llm = client
    except Exception:  # noqa: BLE001
        pass

    autofill = AutofillEngine(prefs.applicant, app_state.settings.data_dir / "screenshots")
    service = ApplicationService(app_state.session_factory, prefs, llm=llm, autofill=autofill)
    try:
        await service.submit(application_id)
    except Exception:  # noqa: BLE001
        logger.exception("Background submit failed for application %s", application_id)
