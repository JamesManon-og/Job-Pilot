"""Human-approval review queue: list pending, approve, reject, edit materials.

Approving only changes the status. Opening and filling the form happens in
`jobpilot apply`, in a visible browser, and the human submits it themselves.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from jobpilot.api.deps import SessionDep
from jobpilot.database.orm import ApplicationRow, JobRow
from jobpilot.database.repositories import (
    ApplicationNotFoundError,
    ApplicationRepository,
    InvalidTransitionError,
)
from jobpilot.domain.enums import ApplicationStatus
from jobpilot.domain.models import Application

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/review", tags=["review"])


class ReviewItem(BaseModel):
    application_id: int
    job_id: int
    title: str
    company: str
    source: str
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
async def review_queue(
    session: SessionDep, status: ApplicationStatus = ApplicationStatus.PENDING_REVIEW
) -> list[ReviewItem]:
    """List applications in a given status (default: awaiting human review)."""
    stmt = (
        select(ApplicationRow, JobRow)
        .join(JobRow, JobRow.id == ApplicationRow.job_id)
        .where(ApplicationRow.status == status.value)
        .order_by(ApplicationRow.created_at.desc())
    )
    rows = (await session.execute(stmt)).all()
    return [
        ReviewItem(
            application_id=app.id,
            job_id=job.id,
            title=job.title,
            company=job.company,
            source=job.source,
            application_url=job.application_url,
            status=app.status,
            cover_letter=app.cover_letter,
            answers=app.answers,
            notes=app.notes,
            created_at=app.created_at.isoformat() if app.created_at else "",
        )
        for app, job in rows
    ]


async def _transition(
    session: SessionDep, application_id: int, target: ApplicationStatus
) -> Application:
    try:
        updated = await ApplicationRepository(session).transition(application_id, target)
        await session.commit()
    except ApplicationNotFoundError:
        raise HTTPException(404, "Application not found") from None
    except InvalidTransitionError as exc:
        await session.rollback()
        raise HTTPException(409, str(exc)) from None
    logger.info("Application %s -> %s (dashboard)", application_id, target.value)
    return updated


@router.patch("/{application_id}", response_model=ReviewAction)
async def edit_materials(
    application_id: int, body: EditMaterials, session: SessionDep
) -> ReviewAction:
    """Edit cover letter or answers before approving."""
    try:
        await ApplicationRepository(session).update_materials(
            application_id, cover_letter=body.cover_letter, answers=body.answers
        )
        await session.commit()
    except ApplicationNotFoundError:
        raise HTTPException(404, "Application not found") from None
    except InvalidTransitionError as exc:
        await session.rollback()
        raise HTTPException(
            409, f"Application is {exc.current.value}; only pending_review can be edited"
        ) from None
    return ReviewAction(status="updated", detail="Materials updated")


@router.post("/{application_id}/approve", response_model=ReviewAction)
async def approve(application_id: int, session: SessionDep) -> ReviewAction:
    """Approve an application. Nothing is opened or submitted yet."""
    await _transition(session, application_id, ApplicationStatus.APPROVED)
    return ReviewAction(
        status="approved",
        detail="Approved. Run `jobpilot apply` to open it in a browser, check the "
        "autofilled form, and submit it yourself.",
    )


@router.post("/{application_id}/reject", response_model=ReviewAction)
async def reject(application_id: int, session: SessionDep) -> ReviewAction:
    """Reject an application — it won't be submitted."""
    await _transition(session, application_id, ApplicationStatus.REJECTED)
    return ReviewAction(status="rejected", detail="Application rejected")


@router.post("/{application_id}/unapprove", response_model=ReviewAction)
async def unapprove(application_id: int, session: SessionDep) -> ReviewAction:
    """Send an approved application back to review so its materials can be edited."""
    await _transition(session, application_id, ApplicationStatus.PENDING_REVIEW)
    return ReviewAction(status="pending_review", detail="Returned to the review queue")
