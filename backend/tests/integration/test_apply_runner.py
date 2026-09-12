"""End-to-end `jobpilot apply` flow in a real (headless) browser with routed pages.

Covers approve → claim → open → autofill → human decision, plus recovery from
a crashed session and the daily cap. Nothing touches the network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from playwright.async_api import BrowserContext, Page, Route, async_playwright
from sqlalchemy.ext.asyncio import AsyncEngine

from jobpilot.applications import ApplicationService
from jobpilot.applications.runner import ApplyRunner, Decision, FillState, StaleDecision
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

    async def ask(application: Application, job: Job, state: FillState) -> Decision:
        seen.append(state.result)
        return queue.pop(0)

    return seen, ask


class OneContext:
    """ContextProvider for tests: every job opens in the same context."""

    def __init__(self, context: BrowserContext, adapter: object | None = None) -> None:
        self.context = context
        self.adapter = adapter

    async def for_job(self, job: Job) -> tuple[BrowserContext, object | None]:
        return self.context, self.adapter


def make_runner(
    engine: AsyncEngine,
    tmp_path: Path,
    ask: object,
    context: BrowserContext,
    *,
    adapter: object | None = None,
    proposer: object | None = None,
    paused: object = lambda: False,
    **prefs: object,
) -> tuple[ApplyRunner, ApplicationService]:
    service = ApplicationService(
        create_session_factory(engine),
        UserPreferences(applicant=APPLICANT, **prefs),  # type: ignore[arg-type]
    )
    autofill = AutofillEngine(APPLICANT, tmp_path / "shots")
    runner = ApplyRunner(
        service,
        autofill,
        OneContext(context, adapter),  # type: ignore[arg-type]
        ask=ask,  # type: ignore[arg-type]
        say=lambda _: None,
        proposer=proposer,  # type: ignore[arg-type]
        paused=paused,  # type: ignore[arg-type]
    )
    return runner, service


class TestApplyRunner:
    async def test_human_confirmed_submission_is_recorded(
        self, engine: AsyncEngine, context: BrowserContext, tmp_path: Path
    ) -> None:
        site = Site()
        await context.route("https://jobs.test/**", site.handle)
        seen, ask = scripted(Decision.SUBMITTED)
        runner, service = make_runner(engine, tmp_path, ask, context)
        app_id = await approved_app(engine, service)

        outcome = await runner.process(app_id)

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
        runner, service = make_runner(engine, tmp_path, ask, context)
        app_id = await approved_app(engine, service)

        outcome = await runner.process(app_id)
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
        runner, service = make_runner(engine, tmp_path, ask, context)
        app_id = await approved_app(engine, service)

        outcome = await runner.process(app_id)
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
        runner, service = make_runner(engine, tmp_path, ask, context)
        app_id = await approved_app(engine, service)
        await service.return_to_review(app_id, notes="changed my mind")

        outcome = await runner.process(app_id)
        assert outcome.status is ApplicationStatus.PENDING_REVIEW
        assert site.requests == []

    async def test_daily_cap_stops_the_session(
        self, engine: AsyncEngine, context: BrowserContext, tmp_path: Path
    ) -> None:
        site = Site()
        await context.route("https://jobs.test/**", site.handle)
        _, ask = scripted(Decision.SUBMITTED)
        runner, service = make_runner(engine, tmp_path, ask, context, max_applications_per_day=1)
        first = await approved_app(engine, service, 1)
        second = await approved_app(engine, service, 2)

        assert (await runner.process(first)).status is ApplicationStatus.SUBMITTED
        outcome = await runner.process(second)
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
        runner, service = make_runner(engine, tmp_path, ask, context)
        app_id = await approved_app(engine, service)
        await runner.process(app_id)
        await runner.process(app_id)
        async with create_session_factory(engine)() as session:
            app = await ApplicationRepository(session).get(app_id)
        assert app is not None and app.status is ApplicationStatus.SUBMITTED
        assert "not submitted" not in app.notes


class TestCrashRecovery:
    async def test_stale_awaiting_confirmation_is_resolved_by_asking(
        self, engine: AsyncEngine, context: BrowserContext, tmp_path: Path
    ) -> None:
        """A previous `jobpilot apply` died with the browser open. Only the human
        knows if they clicked submit; never re-open it automatically."""
        _, ask = scripted()
        runner, service = make_runner(engine, tmp_path, ask, context)
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


LISTING = """<h1>Engineer</h1><p>Great job.</p>
<button id="easy-apply" onclick="document.getElementById('form').hidden = false">Easy Apply</button>
<a id="external" href="https://employer.test/apply" target="_blank">Apply on company site</a>
<form id="form" hidden><label for="n">Full name</label><input id="n"></form>"""


class ClickToApplyAdapter:
    """Like a board adapter: reveals the form by clicking the board's Apply button."""

    uses_browser = True

    def __init__(self, fail_first: Exception | None = None) -> None:
        self.calls = 0
        self.fail_first = fail_first

    async def open_application(self, page: Page, job: Job) -> Page:
        self.calls += 1
        await page.goto(job.application_url)
        if self.fail_first is not None and self.calls == 1:
            raise self.fail_first
        await page.click("#easy-apply")
        return page


async def serve_listing(context: BrowserContext) -> None:
    async def listing(route: Route) -> None:
        await route.fulfill(body=LISTING, content_type="text/html")

    async def employer(route: Route) -> None:
        await route.fulfill(
            body='<label for="e">Email</label><input id="e" type="email">',
            content_type="text/html",
        )

    await context.route("https://jobs.test/**", listing)
    await context.route("https://employer.test/**", employer)


class TestAdapterDrivenApply:
    async def test_adapter_reveals_the_form_then_autofill_fills_it(
        self, engine: AsyncEngine, context: BrowserContext, tmp_path: Path
    ) -> None:
        await serve_listing(context)
        seen, ask = scripted(Decision.SUBMITTED)
        runner, service = make_runner(engine, tmp_path, ask, context, adapter=ClickToApplyAdapter())
        outcome = await runner.process(await approved_app(engine, service))
        assert outcome.status is ApplicationStatus.SUBMITTED
        assert seen[0] is not None and seen[0].fields_filled.get("name") == "James Manon"

    async def test_login_wall_keeps_the_window_open_and_recovers(
        self, engine: AsyncEngine, context: BrowserContext, tmp_path: Path
    ) -> None:
        """Session expired mid-apply: not a failure. The human logs in in the same
        window and presses f; JobPilot reopens the form and fills it."""
        from jobpilot.database.repositories import PlatformSessionRepository
        from jobpilot.platforms import SessionExpiredError

        await serve_listing(context)
        states: list[FillState] = []
        queue = [Decision.REFILL, Decision.KEEP]

        async def ask(application: Application, job: Job, state: FillState) -> Decision:
            states.append(state)
            return queue.pop(0)

        adapter = ClickToApplyAdapter(fail_first=SessionExpiredError(JobSource.LINKEDIN))
        runner, service = make_runner(engine, tmp_path, ask, context, adapter=adapter)
        app_id = await approved_app(engine, service)
        async with create_session_factory(engine)() as session:
            from jobpilot.database.repositories import JobRepository as Jobs

            job = await Jobs(session).get(1)
        assert job is not None

        outcome = await runner.process(app_id)
        assert states[0].error and "log in" in states[0].error
        assert states[1].result is not None and states[1].result.fields_filled.get("name")
        assert outcome.status is ApplicationStatus.APPROVED  # kept, not failed
        async with create_session_factory(engine)() as session:
            saved = await PlatformSessionRepository(session).get(job.source)
        assert saved is not None and saved.status.value == "needs_login"

    async def test_refill_follows_the_human_to_the_employers_tab(
        self, engine: AsyncEngine, context: BrowserContext, tmp_path: Path
    ) -> None:
        await serve_listing(context)
        seen: list[FillState] = []

        async def ask(application: Application, job: Job, state: FillState) -> Decision:
            seen.append(state)
            if len(seen) == 1:  # the human clicks "Apply on company site"
                listing_page = [p for p in context.pages if "jobs.test" in p.url][0]
                async with context.expect_page() as new_tab:
                    await listing_page.click("#external")
                await (await new_tab.value).wait_for_load_state()
                return Decision.REFILL
            return Decision.KEEP

        runner, service = make_runner(engine, tmp_path, ask, context)
        await runner.process(await approved_app(engine, service))
        assert seen[1].result is not None
        assert seen[1].result.url.startswith("https://employer.test")
        assert seen[1].result.fields_filled.get("email") == "james@example.com"
        assert all(p.is_closed() for p in context.pages)  # both tabs cleaned up


class FakeProposer:
    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    async def propose(self, job: Job, questions: list[str]) -> dict[str, str]:
        self.asked = questions
        return self.answers


QUESTION_FORM = """<label for="n">Full name</label><input id="n">
<label for="q">Describe a project you're proud of</label><textarea id="q"></textarea>
<label for="dob">Date of birth</label><input id="dob">"""


class TestProposedAnswers:
    async def _serve(self, context: BrowserContext) -> None:
        async def form(route: Route) -> None:
            await route.fulfill(body=QUESTION_FORM, content_type="text/html")

        await context.route("https://jobs.test/**", form)

    async def test_proposals_go_to_review_never_into_the_form(
        self, engine: AsyncEngine, context: BrowserContext, tmp_path: Path
    ) -> None:
        await self._serve(context)
        proposer = FakeProposer({"Describe a project you're proud of": "MoneyApp, a finance app."})
        states: list[FillState] = []

        async def ask(application: Application, job: Job, state: FillState) -> Decision:
            states.append(state)
            return Decision.PROPOSE

        runner, service = make_runner(engine, tmp_path, ask, context, proposer=proposer)
        app_id = await approved_app(engine, service)
        outcome = await runner.process(app_id)

        assert states[0].can_propose
        assert proposer.asked == ["Describe a project you're proud of"]  # not the DOB field
        assert outcome.status is ApplicationStatus.PENDING_REVIEW
        async with create_session_factory(engine)() as session:
            app = await ApplicationRepository(session).get(app_id)
        assert app is not None
        assert app.answers["Describe a project you're proud of"] == "MoneyApp, a finance app."
        assert "proposed 1 answer" in app.notes

    async def test_nothing_proposed_keeps_it_approved(
        self, engine: AsyncEngine, context: BrowserContext, tmp_path: Path
    ) -> None:
        await self._serve(context)
        _, ask = scripted(Decision.PROPOSE)
        runner, service = make_runner(engine, tmp_path, ask, context, proposer=FakeProposer({}))
        outcome = await runner.process(await approved_app(engine, service))
        assert outcome.status is ApplicationStatus.APPROVED

    async def test_propose_unavailable_without_an_llm(
        self, engine: AsyncEngine, context: BrowserContext, tmp_path: Path
    ) -> None:
        await self._serve(context)
        states: list[FillState] = []

        async def ask(application: Application, job: Job, state: FillState) -> Decision:
            states.append(state)
            return Decision.PROPOSE  # ignored: not offered

        runner, service = make_runner(engine, tmp_path, ask, context)
        outcome = await runner.process(await approved_app(engine, service))
        assert states[0].can_propose is False
        assert outcome.status is ApplicationStatus.APPROVED


class TestSessionControls:
    async def test_pause_stops_before_opening_anything(
        self, engine: AsyncEngine, context: BrowserContext, tmp_path: Path
    ) -> None:
        site = Site()
        await context.route("https://jobs.test/**", site.handle)
        _, ask = scripted()
        runner, service = make_runner(engine, tmp_path, ask, context, paused=lambda: True)
        outcome = await runner.process(await approved_app(engine, service))
        assert outcome.stop and outcome.detail == "paused"
        assert site.requests == []

    async def test_platform_cap_skips_only_that_platform(
        self, engine: AsyncEngine, context: BrowserContext, tmp_path: Path
    ) -> None:
        from jobpilot.config.preferences import PlatformSettings

        await context.route("https://jobs.test/**", Site().handle)
        _, ask = scripted(Decision.SUBMITTED, Decision.SUBMITTED)
        runner, service = make_runner(
            engine,
            tmp_path,
            ask,
            context,
            platforms={"remoteok": PlatformSettings(enabled=True, max_applications_per_day=1)},
        )
        first = await approved_app(engine, service, 1)
        second = await approved_app(engine, service, 2)
        assert (await runner.process(first)).status is ApplicationStatus.SUBMITTED
        capped = await runner.process(second)
        assert capped.stop is False  # other platforms may continue
        assert "remoteok cap" in capped.detail
