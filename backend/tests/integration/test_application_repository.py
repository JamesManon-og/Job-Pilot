from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from jobpilot.database import create_session_factory
from jobpilot.database.repositories import (
    ApplicationRepository,
    DailyCapReachedError,
    DuplicateApplicationError,
    InvalidTransitionError,
    JobRepository,
    ResumeRepository,
)
from jobpilot.domain import (
    APPLICATION_TRANSITIONS,
    Application,
    ApplicationStatus,
    Job,
    JobSource,
    Resume,
)

S = ApplicationStatus


async def seed_job_and_resume(session: AsyncSession, n: int = 1) -> tuple[int, int]:
    job, _ = await JobRepository(session).upsert(
        Job(
            title=f"Engineer {n}",
            company="Acme",
            application_url=f"https://a.com/j/{n}",
            source=JobSource.REMOTEOK,
        )
    )
    resume = await ResumeRepository(session).get_by_version("v1") or await ResumeRepository(
        session
    ).add(Resume(version="v1", file_path="/resumes/v1.pdf"))
    assert job.id is not None and resume.id is not None
    return job.id, resume.id


async def walk(repo: ApplicationRepository, app_id: int, *statuses: ApplicationStatus) -> None:
    for status in statuses:
        await repo.transition(app_id, status)


class TestApplicationRepository:
    async def test_create_and_fetch(self, session: AsyncSession) -> None:
        job_id, resume_id = await seed_job_and_resume(session)
        repo = ApplicationRepository(session)
        app = await repo.create(Application(job_id=job_id, resume_id=resume_id))
        assert app.id is not None
        assert app.status is ApplicationStatus.PENDING_REVIEW

    async def test_duplicate_application_rejected(self, session: AsyncSession) -> None:
        job_id, resume_id = await seed_job_and_resume(session)
        repo = ApplicationRepository(session)
        await repo.create(Application(job_id=job_id, resume_id=resume_id))
        with pytest.raises(DuplicateApplicationError):
            await repo.create(Application(job_id=job_id, resume_id=resume_id))

    async def test_company_position_duplicate_check(self, session: AsyncSession) -> None:
        job_id, resume_id = await seed_job_and_resume(session)
        repo = ApplicationRepository(session)
        assert not await repo.exists_for_company_position("Acme", "Engineer 1")
        await repo.create(Application(job_id=job_id, resume_id=resume_id))
        assert await repo.exists_for_company_position("  ACME ", "engineer 1")

    async def test_full_lifecycle_sets_submitted_at_and_logs_events(
        self, session: AsyncSession
    ) -> None:
        job_id, resume_id = await seed_job_and_resume(session)
        repo = ApplicationRepository(session)
        app = await repo.create(Application(job_id=job_id, resume_id=resume_id))
        assert app.id is not None

        await walk(repo, app.id, S.APPROVED, S.AWAITING_CONFIRMATION)
        updated = await repo.transition(app.id, S.SUBMITTED)
        assert updated.status is S.SUBMITTED
        assert updated.submitted_at is not None
        assert updated.submitted_at.tzinfo is not None

        events = await repo.list_events(app.id)
        moves = [(e["payload"]["from"], e["payload"]["to"]) for e in events[1:]]
        assert moves == [
            ("pending_review", "approved"),
            ("approved", "awaiting_confirmation"),
            ("awaiting_confirmation", "submitted"),
        ]

    async def test_list_by_status(self, session: AsyncSession) -> None:
        job_id, resume_id = await seed_job_and_resume(session)
        repo = ApplicationRepository(session)
        app = await repo.create(Application(job_id=job_id, resume_id=resume_id))
        assert app.id is not None
        await walk(repo, app.id, S.APPROVED, S.AWAITING_CONFIRMATION, S.SUBMITTED)

        assert await repo.list(status=ApplicationStatus.PENDING_REVIEW) == []
        submitted = await repo.list(status=ApplicationStatus.SUBMITTED)
        assert [a.id for a in submitted] == [app.id]


class TestLifecycleCannotBeSkipped:
    """Regression: update_status() used to accept any status from any status,
    so PENDING_REVIEW -> SUBMITTED (skipping approval) was one call away."""

    @pytest.mark.parametrize("target", [S.SUBMITTED, S.AWAITING_CONFIRMATION, S.FAILED])
    async def test_pending_review_cannot_jump_past_approval(
        self, session: AsyncSession, target: ApplicationStatus
    ) -> None:
        job_id, resume_id = await seed_job_and_resume(session)
        repo = ApplicationRepository(session)
        app = await repo.create(Application(job_id=job_id, resume_id=resume_id))
        assert app.id is not None
        with pytest.raises(InvalidTransitionError):
            await repo.transition(app.id, target)
        with pytest.raises(InvalidTransitionError):
            await repo.update_status(app.id, target)
        assert (await repo.get(app.id)).status is S.PENDING_REVIEW  # type: ignore[union-attr]

    @pytest.mark.parametrize("terminal", [S.SUBMITTED, S.REJECTED])
    async def test_terminal_states_are_final(
        self, session: AsyncSession, terminal: ApplicationStatus
    ) -> None:
        job_id, resume_id = await seed_job_and_resume(session)
        repo = ApplicationRepository(session)
        app = await repo.create(Application(job_id=job_id, resume_id=resume_id))
        assert app.id is not None
        path = (
            [S.APPROVED, S.AWAITING_CONFIRMATION, S.SUBMITTED]
            if terminal is S.SUBMITTED
            else [S.REJECTED]
        )
        await walk(repo, app.id, *path)
        for target in S:
            if target is terminal:
                continue
            with pytest.raises(InvalidTransitionError):
                await repo.transition(app.id, target)

    def test_submitted_is_only_reachable_from_awaiting_confirmation(self) -> None:
        sources = [s for s, targets in APPLICATION_TRANSITIONS.items() if S.SUBMITTED in targets]
        assert sources == [S.AWAITING_CONFIRMATION]

    async def test_editing_materials_after_approval_is_refused(self, session: AsyncSession) -> None:
        job_id, resume_id = await seed_job_and_resume(session)
        repo = ApplicationRepository(session)
        app = await repo.create(Application(job_id=job_id, resume_id=resume_id))
        assert app.id is not None
        await repo.update_materials(app.id, cover_letter="v2")
        await repo.transition(app.id, S.APPROVED)
        with pytest.raises(InvalidTransitionError):
            await repo.update_materials(app.id, cover_letter="sneaky edit")
        assert (await repo.get(app.id)).cover_letter == "v2"  # type: ignore[union-attr]


class TestConcurrency:
    async def test_two_concurrent_approvals_exactly_one_wins(self, engine: AsyncEngine) -> None:
        """Two tabs / a double-click: both requests read pending_review."""
        factory = create_session_factory(engine)
        async with factory() as session:
            job_id, resume_id = await seed_job_and_resume(session)
            app = await ApplicationRepository(session).create(
                Application(job_id=job_id, resume_id=resume_id)
            )
            await session.commit()
        assert app.id is not None

        async def approve() -> str:
            async with factory() as session:
                try:
                    await ApplicationRepository(session).transition(app.id, S.APPROVED)  # type: ignore[arg-type]
                    await session.commit()
                    return "won"
                except InvalidTransitionError:
                    await session.rollback()
                    return "lost"

        results = await asyncio.gather(*(approve() for _ in range(5)))
        assert sorted(results) == ["lost"] * 4 + ["won"]
        async with factory() as session:
            events = await ApplicationRepository(session).list_events(app.id)
        assert [e["event_type"] for e in events].count("status_changed") == 1

    async def test_stale_read_cannot_double_submit(self, engine: AsyncEngine) -> None:
        """Compare-and-set: a second worker that validated against a stale status fails."""
        factory = create_session_factory(engine)
        async with factory() as session:
            job_id, resume_id = await seed_job_and_resume(session)
            repo = ApplicationRepository(session)
            app = await repo.create(Application(job_id=job_id, resume_id=resume_id))
            assert app.id is not None
            await walk(repo, app.id, S.APPROVED, S.AWAITING_CONFIRMATION)
            await session.commit()

        async with factory() as first, factory() as second:
            await ApplicationRepository(first).transition(app.id, S.SUBMITTED)
            await first.commit()
            with pytest.raises(InvalidTransitionError):
                await ApplicationRepository(second).transition(app.id, S.SUBMITTED)

    async def test_integrity_error_race_becomes_duplicate_error(
        self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Another process inserts between our pre-check and our INSERT."""
        factory = create_session_factory(engine)
        async with factory() as session:
            job_id, resume_id = await seed_job_and_resume(session)
            await ApplicationRepository(session).create(
                Application(job_id=job_id, resume_id=resume_id)
            )
            await session.commit()

        async def no_precheck(self: ApplicationRepository, job_id: int) -> bool:
            return False

        monkeypatch.setattr(ApplicationRepository, "exists_for_job", no_precheck)
        async with factory() as session:
            with pytest.raises(DuplicateApplicationError):
                await ApplicationRepository(session).create(
                    Application(job_id=job_id, resume_id=resume_id)
                )


class TestDailyCapClaim:
    async def _approved(self, session: AsyncSession, n: int) -> int:
        job_id, resume_id = await seed_job_and_resume(session, n)
        repo = ApplicationRepository(session)
        app = await repo.create(Application(job_id=job_id, resume_id=resume_id))
        assert app.id is not None
        await repo.transition(app.id, S.APPROVED)
        return app.id

    async def test_claim_counts_in_flight_applications(self, session: AsyncSession) -> None:
        repo = ApplicationRepository(session)
        first, second = await self._approved(session, 1), await self._approved(session, 2)
        day_start = datetime.now(UTC) - timedelta(hours=1)

        claimed = await repo.claim_for_autofill(first, daily_cap=1, day_start=day_start)
        assert claimed.status is S.AWAITING_CONFIRMATION
        with pytest.raises(DailyCapReachedError):
            await repo.claim_for_autofill(second, daily_cap=1, day_start=day_start)
        assert (await repo.get(second)).status is S.APPROVED  # type: ignore[union-attr]

    async def test_submissions_before_today_do_not_count(self, session: AsyncSession) -> None:
        repo = ApplicationRepository(session)
        first, second = await self._approved(session, 1), await self._approved(session, 2)
        await repo.claim_for_autofill(first, daily_cap=5, day_start=datetime.now(UTC))
        await repo.transition(first, S.SUBMITTED)

        tomorrow = datetime.now(UTC) + timedelta(days=1)
        claimed = await repo.claim_for_autofill(second, daily_cap=1, day_start=tomorrow)
        assert claimed.status is S.AWAITING_CONFIRMATION

    async def test_claiming_twice_is_refused(self, session: AsyncSession) -> None:
        repo = ApplicationRepository(session)
        app_id = await self._approved(session, 1)
        await repo.claim_for_autofill(app_id, daily_cap=5, day_start=datetime.now(UTC))
        with pytest.raises(InvalidTransitionError):
            await repo.claim_for_autofill(app_id, daily_cap=5, day_start=datetime.now(UTC))

    async def test_failed_application_can_be_retried(self, session: AsyncSession) -> None:
        repo = ApplicationRepository(session)
        app_id = await self._approved(session, 1)
        await repo.claim_for_autofill(app_id, daily_cap=5, day_start=datetime.now(UTC))
        await repo.transition(app_id, S.FAILED, notes="HTTP 503")
        retried = await repo.claim_for_autofill(app_id, daily_cap=5, day_start=datetime.now(UTC))
        assert retried.status is S.AWAITING_CONFIRMATION
