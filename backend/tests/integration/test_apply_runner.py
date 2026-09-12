"""End-to-end `jobpilot apply` flow in a real (headless) browser with routed pages.

Covers approve → claim → open → autofill → human decision, plus recovery from
a crashed session and the daily cap. Nothing touches the network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from playwright.async_api import BrowserContext, Route, async_playwright
from sqlalchemy.ext.asyncio import AsyncEngine

from jobpilot.applications import ApplicationService
from jobpilot.applications.runner import ApplyRunner, Decision, StaleDecision
from jobpilot.autofill import AutofillEngine, FilledForm
from jobpilot.config.preferences import ApplicantProfile, UserPreferences
from jobpilot.database import create_session_factory
from jobpilot.database.repositories import (
    ApplicationRepository,
    JobRepository,
    ResumeRepository,
)
from jobpilot.domain import ApplicationStatus, Job, JobSource, Resume
from jobpilot.domain.models import Application

FORM = (Path(__file__).resolve().parents[1] / "fixtures" / "application_form.html").read_text()
APPLICANT = ApplicantProfile(name="James Manon", email="james@example.com", phone="+63 900")


@pytest.fixture
async def context() -> AsyncIterator[BrowserContext]:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context()
        yield ctx
        await browser.close()


class Site:
    """Routes https://jobs.test/* to the fixture form and records every request."""

    def __init__(self, status: int = 200) -> None:
        self.requests: list[str] = []
        self.status = status

    async def handle(self, route: Route) -> None:
        self.requests.append(f"{route.request.method} {route.request.url}")
        await route.fulfill(status=self.status, body=FORM, content_type="text/html")


async def approved_app(engine: AsyncEngine, service: ApplicationService, n: int = 1) -> int:
    factory = create_session_factory(engine)
    async with factory() as session:
        job, _ = await JobRepository(session).upsert(
            Job(
                title=f"Engineer {n}",
                company="Acme",
                application_url=f"https://jobs.test/apply/{n}",
                source=JobSource.REMOTEOK,
            )
        )
        resume = await ResumeRepository(session).get_by_version("v1") or await ResumeRepository(
            session
        ).add(Resume(version="v1", file_path="/r.pdf", is_active=True))
        await session.commit()
    assert job.id is not None and resume.id is not None
    app = await service.prepare(job.id, resume_id=resume.id)
    assert app.id is not None
    await service.approve(app.id)
    return app.id


def scripted(*decisions: Decision) -> tuple[list[FilledForm | None], object]:
    seen: list[FilledForm | None] = []
    queue = list(decisions)

    async def ask(
        application: Application, job: Job, result: FilledForm | None, error: str | None
    ) -> Decision:
        seen.append(result)
        return queue.pop(0)

    return seen, ask


def make_runner(
    engine: AsyncEngine, tmp_path: Path, ask: object, **prefs: object
) -> tuple[ApplyRunner, ApplicationService]:
    service = ApplicationService(
        create_session_factory(engine),
        UserPreferences(applicant=APPLICANT, **prefs),  # type: ignore[arg-type]
    )
    autofill = AutofillEngine(APPLICANT, tmp_path / "shots")
    return ApplyRunner(service, autofill, ask=ask, say=lambda _: None), service  # type: ignore[arg-type]


class TestApplyRunner:
    async def test_human_confirmed_submission_is_recorded(
        self, engine: AsyncEngine, context: BrowserContext, tmp_path: Path
    ) -> None:
        site = Site()
        await context.route("https://jobs.test/**", site.handle)
        seen, ask = scripted(Decision.SUBMITTED)
        runner, service = make_runner(engine, tmp_path, ask)
        app_id = await approved_app(engine, service)

        outcome = await runner.process(context, app_id)

        assert outcome.status is ApplicationStatus.SUBMITTED
        assert seen[0] is not None and seen[0].fields_filled["name"] == "James Manon"
        # JobPilot loaded the page once and never submitted the form itself.
        assert site.requests == ["GET https://jobs.test/apply/1"]
        async with create_session_factory(engine)() as session:
            app = await ApplicationRepository(session).get(app_id)
            events = await ApplicationRepository(session).list_events(app_id)
        assert app is not None and app.screenshot_path and Path(app.screenshot_path).exists()
        assert [e["event_type"] for e in events].count("autofilled") == 1
        submitted = [e for e in events if e["payload"].get("to") == "submitted"]
        assert submitted[0]["payload"]["confirmed_by"] == "user"

    async def test_refill_then_keep_leaves_it_approved(
        self, engine: AsyncEngine, context: BrowserContext, tmp_path: Path
    ) -> None:
        await context.route("https://jobs.test/**", Site().handle)
        seen, ask = scripted(Decision.REFILL, Decision.KEEP)
        runner, service = make_runner(engine, tmp_path, ask)
        app_id = await approved_app(engine, service)

        outcome = await runner.process(context, app_id)
        assert outcome.status is ApplicationStatus.APPROVED
        assert len(seen) == 2
        async with create_session_factory(engine)() as session:
            events = await ApplicationRepository(session).list_events(app_id)
        assert [e["event_type"] for e in events].count("autofilled") == 2

    async def test_removed_posting_marks_failed_with_reason(
        self, engine: AsyncEngine, context: BrowserContext, tmp_path: Path
    ) -> None:
        await context.route("https://jobs.test/**", Site(status=404).handle)
        _, ask = scripted()
        runner, service = make_runner(engine, tmp_path, ask)
        app_id = await approved_app(engine, service)

        outcome = await runner.process(context, app_id)
        assert outcome.status is ApplicationStatus.FAILED
        async with create_session_factory(engine)() as session:
            app = await ApplicationRepository(session).get(app_id)
        assert app is not None and "404" in app.notes

    async def test_unapproved_application_is_never_opened(
        self, engine: AsyncEngine, context: BrowserContext, tmp_path: Path
    ) -> None:
        site = Site()
        await context.route("https://jobs.test/**", site.handle)
        _, ask = scripted()
        runner, service = make_runner(engine, tmp_path, ask)
        app_id = await approved_app(engine, service)
        await service.return_to_review(app_id, notes="changed my mind")

        outcome = await runner.process(context, app_id)
        assert outcome.status is ApplicationStatus.PENDING_REVIEW
        assert site.requests == []

    async def test_daily_cap_stops_the_session(
        self, engine: AsyncEngine, context: BrowserContext, tmp_path: Path
    ) -> None:
        site = Site()
        await context.route("https://jobs.test/**", site.handle)
        _, ask = scripted(Decision.SUBMITTED)
        runner, service = make_runner(engine, tmp_path, ask, max_applications_per_day=1)
        first = await approved_app(engine, service, 1)
        second = await approved_app(engine, service, 2)

        assert (await runner.process(context, first)).status is ApplicationStatus.SUBMITTED
        outcome = await runner.process(context, second)
        assert outcome.stop is True
        assert "Daily cap" in outcome.detail
        assert site.requests == ["GET https://jobs.test/apply/1"]


class TestNotesStayTruthful:
    async def test_keep_then_submit_does_not_leave_a_not_submitted_note(
        self, engine: AsyncEngine, context: BrowserContext, tmp_path: Path
    ) -> None:
        """Bug found in the manual E2E run: after "keep for later" then a later
        confirmed submission, the UI showed SUBMITTED with the note "not
        submitted yet"."""
        await context.route("https://jobs.test/**", Site().handle)
        _, ask = scripted(Decision.KEEP, Decision.SUBMITTED)
        runner, service = make_runner(engine, tmp_path, ask)
        app_id = await approved_app(engine, service)
        await runner.process(context, app_id)
        await runner.process(context, app_id)
        async with create_session_factory(engine)() as session:
            app = await ApplicationRepository(session).get(app_id)
        assert app is not None and app.status is ApplicationStatus.SUBMITTED
        assert "not submitted" not in app.notes


class TestCrashRecovery:
    async def test_stale_awaiting_confirmation_is_resolved_by_asking(
        self, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        """A previous `jobpilot apply` died with the browser open. Only the human
        knows if they clicked submit; never re-open it automatically."""
        _, ask = scripted()
        runner, service = make_runner(engine, tmp_path, ask)
        submitted_id = await approved_app(engine, service, 1)
        abandoned_id = await approved_app(engine, service, 2)
        stale = [await service.claim_for_autofill(i) for i in (submitted_id, abandoned_id)]
        answers = {submitted_id: StaleDecision.SUBMITTED, abandoned_id: StaleDecision.NOT_SUBMITTED}

        async def ask_stale(application: Application, job: Job) -> StaleDecision:
            assert application.id is not None
            return answers[application.id]

        outcomes = await runner.resolve_stale(stale, ask_stale)
        assert {o.application_id: o.status for o in outcomes} == {
            submitted_id: ApplicationStatus.SUBMITTED,
            abandoned_id: ApplicationStatus.APPROVED,
        }
