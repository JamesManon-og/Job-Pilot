"""Application persistence with duplicate prevention and an audit event log.

Status changes are compare-and-set UPDATEs validated against the domain's
APPLICATION_TRANSITIONS table. Two concurrent requests (double-clicks, two
tabs, a CLI and the dashboard) can never both win the same transition, so an
application cannot be approved, claimed, or submitted twice.
"""

from __future__ import annotations

import builtins
from datetime import datetime
from typing import Any

from sqlalchemy import func, literal, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.database.orm import ApplicationEventRow, ApplicationRow, JobRow, UTCDateTime
from jobpilot.domain.enums import ApplicationStatus, JobStatus
from jobpilot.domain.identity import job_fingerprint
from jobpilot.domain.models import Application, can_transition, utcnow

_JOB_STATUS_FOR = {
    ApplicationStatus.SUBMITTED: JobStatus.APPLIED,
    ApplicationStatus.REJECTED: JobStatus.REJECTED,
    ApplicationStatus.SKIPPED: JobStatus.SKIPPED,
}


class DuplicateApplicationError(Exception):
    """Raised when an application already exists for the same job."""

    def __init__(self, job_id: int, *, reason: str = "") -> None:
        super().__init__(reason or f"An application already exists for job_id={job_id}")
        self.job_id = job_id


class ApplicationNotFoundError(LookupError):
    def __init__(self, application_id: int) -> None:
        super().__init__(f"Application {application_id} not found")
        self.application_id = application_id


class InvalidTransitionError(Exception):
    """The application is not in a state that allows the requested change."""

    def __init__(
        self, application_id: int, current: ApplicationStatus, target: ApplicationStatus
    ) -> None:
        super().__init__(
            f"Application {application_id} is {current.value}; cannot move it to {target.value}"
        )
        self.application_id = application_id
        self.current = current
        self.target = target


class DailyCapReachedError(Exception):
    def __init__(self, cap: int, used_today: int, *, platform: str | None = None) -> None:
        scope = f"{platform} " if platform else ""
        super().__init__(
            f"Daily {scope}cap reached: {used_today}/{cap} {scope}applications submitted or "
            "in progress today"
        )
        self.cap = cap
        self.used_today = used_today
        self.platform = platform


def _to_domain(row: ApplicationRow) -> Application:
    return Application(
        id=row.id,
        job_id=row.job_id,
        resume_id=row.resume_id,
        status=ApplicationStatus(row.status),
        cover_letter=row.cover_letter,
        answers=row.answers,
        screenshot_path=row.screenshot_path,
        external_application_id=row.external_application_id,
        notes=row.notes,
        submitted_at=row.submitted_at,
        created_at=row.created_at,
    )


def _counts_toward_cap(since: datetime) -> Any:
    """Rows using up today's quota: submitted since `since`, or open in a browser now."""
    return or_(
        ApplicationRow.submitted_at >= since,
        ApplicationRow.status == ApplicationStatus.AWAITING_CONFIRMATION.value,
    )


class ApplicationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, application: Application) -> Application:
        """Persist a new application; refuses duplicates for the same job.

        The pre-check gives a clean error in the common case; the UNIQUE(job_id)
        constraint is what actually guarantees it under concurrent writers.
        """
        if await self.exists_for_job(application.job_id):
            raise DuplicateApplicationError(application.job_id)

        row = ApplicationRow(
            job_id=application.job_id,
            resume_id=application.resume_id,
            status=application.status.value,
            cover_letter=application.cover_letter,
            answers=application.answers,
            screenshot_path=application.screenshot_path,
            external_application_id=application.external_application_id,
            notes=application.notes,
            submitted_at=application.submitted_at,
            created_at=application.created_at,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            # Lost a race with another process preparing the same job. The
            # caller must roll back the session before reusing it.
            raise DuplicateApplicationError(application.job_id) from exc
        await self.log_event(row.id, "created", {"status": row.status})
        await self._sync_job_status(application.job_id, application.status)
        return _to_domain(row)

    async def _sync_job_status(self, job_id: int, status: ApplicationStatus) -> None:
        """The job's pipeline status follows its application's."""
        job_status = _JOB_STATUS_FOR.get(status, JobStatus.PREPARED)
        await self._session.execute(
            update(JobRow).where(JobRow.id == job_id).values(status=job_status.value)
        )

    async def exists_for_job(self, job_id: int) -> bool:
        result = await self._session.scalar(
            select(func.count()).select_from(ApplicationRow).where(ApplicationRow.job_id == job_id)
        )
        return bool(result)

    async def exists_for_company_position(self, company: str, title: str) -> bool:
        """Cross-source duplicate check: an application for the same job on any board.

        Compares normalized fingerprints, so "Acme Inc." / "ACME" and
        "Sr. Engineer (Remote)" / "Senior Engineer" count as the same job.
        """
        result = await self._session.scalar(
            select(func.count())
            .select_from(ApplicationRow)
            .join(JobRow, JobRow.id == ApplicationRow.job_id)
            .where(JobRow.fingerprint == job_fingerprint(company, title))
        )
        return bool(result)

    async def get(self, application_id: int) -> Application | None:
        # populate_existing: status changes happen via UPDATE statements, so an
        # identity-map copy loaded earlier in this session may be stale.
        row = await self._session.get(ApplicationRow, application_id, populate_existing=True)
        return _to_domain(row) if row else None

    async def list(
        self, *, status: ApplicationStatus | None = None, limit: int = 100, offset: int = 0
    ) -> list[Application]:
        stmt = (
            select(ApplicationRow)
            .order_by(ApplicationRow.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if status is not None:
            stmt = stmt.where(ApplicationRow.status == status.value)
        rows = await self._session.scalars(stmt)
        return [_to_domain(row) for row in rows]

    async def _current_status(self, application_id: int) -> ApplicationStatus:
        value = await self._session.scalar(
            select(ApplicationRow.status).where(ApplicationRow.id == application_id)
        )
        if value is None:
            raise ApplicationNotFoundError(application_id)
        return ApplicationStatus(value)

    async def transition(
        self,
        application_id: int,
        target: ApplicationStatus,
        *,
        notes: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Application:
        """Atomically move an application to `target` if the lifecycle allows it.

        Compare-and-set: the UPDATE only matches if the status is still the one
        we validated, so of two concurrent callers exactly one succeeds and the
        other gets InvalidTransitionError.
        """
        current = await self._current_status(application_id)
        if not can_transition(current, target):
            raise InvalidTransitionError(application_id, current, target)

        values: dict[str, Any] = {"status": target.value}
        if notes is not None:
            values["notes"] = notes
        if target is ApplicationStatus.SUBMITTED:
            values["submitted_at"] = func.coalesce(
                ApplicationRow.submitted_at, literal(utcnow(), UTCDateTime())
            )
        result = await self._session.execute(
            update(ApplicationRow)
            .where(ApplicationRow.id == application_id, ApplicationRow.status == current.value)
            .values(**values)
            .execution_options(synchronize_session="fetch")
        )
        if getattr(result, "rowcount", 0) != 1:
            raise InvalidTransitionError(
                application_id, await self._current_status(application_id), target
            )
        await self.log_event(
            application_id,
            "status_changed",
            {"from": current.value, "to": target.value, **(payload or {})},
        )
        updated = await self.get(application_id)
        assert updated is not None
        await self._sync_job_status(updated.job_id, target)
        return updated

    async def update_status(
        self, application_id: int, status: ApplicationStatus, *, notes: str | None = None
    ) -> Application | None:
        """Validated status change; returns None if the application doesn't exist."""
        try:
            return await self.transition(application_id, status, notes=notes)
        except ApplicationNotFoundError:
            return None

    async def claim_for_autofill(
        self,
        application_id: int,
        *,
        daily_cap: int,
        day_start: datetime,
        platform_caps: dict[str, int] | None = None,
    ) -> Application:
        """APPROVED/FAILED -> AWAITING_CONFIRMATION, only while under the daily caps.

        The cap checks and the status change are one UPDATE statement, so two
        `jobpilot apply` processes can't both squeeze past a cap. `platform_caps`
        adds per-platform limits (jobs.source -> max per day) on top of the
        global one.
        """
        current = await self._current_status(application_id)
        target = ApplicationStatus.AWAITING_CONFIRMATION
        if current is ApplicationStatus.FAILED:
            # A failed attempt goes back through APPROVED; approval still stands.
            await self.transition(application_id, ApplicationStatus.APPROVED)
            current = ApplicationStatus.APPROVED
        if not can_transition(current, target):
            raise InvalidTransitionError(application_id, current, target)

        conditions = [
            ApplicationRow.id == application_id,
            ApplicationRow.status == current.value,
            self._used_today(day_start) < daily_cap,
        ]
        source = await self._session.scalar(
            select(JobRow.source)
            .join(ApplicationRow, ApplicationRow.job_id == JobRow.id)
            .where(ApplicationRow.id == application_id)
        )
        platform_cap = (platform_caps or {}).get(source or "")
        if platform_cap is not None:
            conditions.append(self._used_today(day_start, source=source) < platform_cap)
        result = await self._session.execute(
            update(ApplicationRow)
            .where(*conditions)
            .values(status=target.value)
            .execution_options(synchronize_session="fetch")
        )
        if getattr(result, "rowcount", 0) != 1:
            now_status = await self._current_status(application_id)
            if now_status is not current:
                raise InvalidTransitionError(application_id, now_status, target)
            used = await self.count_toward_cap(day_start)
            if used < daily_cap and platform_cap is not None:
                raise DailyCapReachedError(
                    platform_cap,
                    await self.count_toward_cap(day_start, source=source),
                    platform=source,
                )
            raise DailyCapReachedError(daily_cap, used)
        await self.log_event(
            application_id, "status_changed", {"from": current.value, "to": target.value}
        )
        claimed = await self.get(application_id)
        assert claimed is not None
        return claimed

    def _used_today(self, day_start: datetime, *, source: str | None = None) -> Any:
        stmt = select(func.count()).select_from(ApplicationRow).where(_counts_toward_cap(day_start))
        if source is not None:
            stmt = stmt.join(JobRow, JobRow.id == ApplicationRow.job_id).where(
                JobRow.source == source
            )
        return stmt.scalar_subquery()

    async def update_materials(
        self,
        application_id: int,
        *,
        cover_letter: str | None = None,
        answers: dict[str, str] | None = None,
    ) -> Application:
        """Edit materials; only allowed while the application is still in review."""
        values: dict[str, Any] = {}
        if cover_letter is not None:
            values["cover_letter"] = cover_letter
        if answers is not None:
            values["answers"] = answers
        current = await self._current_status(application_id)
        if current is not ApplicationStatus.PENDING_REVIEW:
            raise InvalidTransitionError(application_id, current, ApplicationStatus.PENDING_REVIEW)
        if values:
            result = await self._session.execute(
                update(ApplicationRow)
                .where(
                    ApplicationRow.id == application_id,
                    ApplicationRow.status == ApplicationStatus.PENDING_REVIEW.value,
                )
                .values(**values)
                .execution_options(synchronize_session="fetch")
            )
            if getattr(result, "rowcount", 0) != 1:  # approved/rejected in the meantime
                raise InvalidTransitionError(
                    application_id,
                    await self._current_status(application_id),
                    ApplicationStatus.PENDING_REVIEW,
                )
            await self.log_event(application_id, "materials_edited", {"fields": sorted(values)})
        updated = await self.get(application_id)
        assert updated is not None
        return updated

    async def set_screenshot(self, application_id: int, path: str) -> None:
        row = await self._session.get(ApplicationRow, application_id)
        if row is not None:
            row.screenshot_path = path
            await self._session.flush()

    async def count_submitted_since(self, since: datetime) -> int:
        """How many applications were submitted after `since`."""
        result = await self._session.scalar(
            select(func.count())
            .select_from(ApplicationRow)
            .where(ApplicationRow.submitted_at.is_not(None), ApplicationRow.submitted_at >= since)
        )
        return result or 0

    async def count_toward_cap(self, since: datetime, *, source: str | None = None) -> int:
        """Submitted since `since` plus those currently open for submission."""
        stmt = select(func.count()).select_from(ApplicationRow).where(_counts_toward_cap(since))
        if source is not None:
            stmt = stmt.join(JobRow, JobRow.id == ApplicationRow.job_id).where(
                JobRow.source == source
            )
        result = await self._session.scalar(stmt)
        return result or 0

    async def job_ids_with_applications(self) -> set[int]:
        rows = await self._session.scalars(select(ApplicationRow.job_id))
        return set(rows)

    async def list_events(self, application_id: int) -> builtins.list[dict[str, Any]]:
        rows = await self._session.scalars(
            select(ApplicationEventRow)
            .where(ApplicationEventRow.application_id == application_id)
            .order_by(ApplicationEventRow.id)
        )
        return [
            {"event_type": row.event_type, "payload": row.payload, "created_at": row.created_at}
            for row in rows
        ]

    async def log_event(
        self, application_id: int, event_type: str, payload: dict[str, Any] | None = None
    ) -> None:
        self._session.add(
            ApplicationEventRow(
                application_id=application_id,
                event_type=event_type,
                payload=payload or {},
                created_at=utcnow(),
            )
        )
        await self._session.flush()
