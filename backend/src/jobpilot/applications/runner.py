"""`jobpilot apply`: open approved applications in a visible browser and wait for the human.

Flow per application:
    APPROVED --claim--> AWAITING_CONFIRMATION --(human submits in the browser,
    then confirms here)--> SUBMITTED

The job's platform adapter opens it in that platform's logged-in browser
profile and reveals the form (e.g. clicks LinkedIn's Easy Apply); jobs on
employer sites open in a fresh browser. JobPilot autofills what it can
identify. The human checks every field, handles anything flagged (unknown
questions, sensitive fields, CAPTCHAs, logins), clicks the site's own submit
button, and then tells JobPilot. SUBMITTED is recorded only on that explicit
confirmation — JobPilot never clicks submit.

I/O goes through the `ask`/`say` callables so the flow is testable without a
terminal.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import TracebackType
from typing import Protocol

from playwright.async_api import BrowserContext, Page, async_playwright
from playwright.async_api import Error as PlaywrightError

from jobpilot.applications.answers import AnswerProposer
from jobpilot.applications.service import ApplicationService
from jobpilot.autofill.engine import AutofillEngine, AutofillError, FilledForm
from jobpilot.config.preferences import UserPreferences
from jobpilot.database.repositories import DailyCapReachedError, InvalidTransitionError
from jobpilot.domain.enums import ApplicationStatus, JobSource, SessionStatus
from jobpilot.domain.models import Application, Job
from jobpilot.platforms.base import (
    ChallengeError,
    PlatformAdapter,
    PlatformError,
    SessionExpiredError,
)
from jobpilot.platforms.browser import BrowserProfiles
from jobpilot.platforms.registry import all_adapter_classes

logger = logging.getLogger(__name__)


class Decision(StrEnum):
    SUBMITTED = "submitted"  # the human submitted it on the site
    REFILL = "refill"  # fill the current page again (e.g. after clicking "Next")
    PROPOSE = "propose"  # LLM drafts answers for unknown questions → back to review
    KEEP = "keep"  # not now: back to the approved queue
    REJECT = "reject"
    QUIT = "quit"  # stop this session; current application goes back to approved


class StaleDecision(StrEnum):
    SUBMITTED = "submitted"
    NOT_SUBMITTED = "not_submitted"
    REJECT = "reject"


@dataclass
class ApplyOutcome:
    application_id: int
    status: ApplicationStatus | None
    detail: str
    stop: bool = False


@dataclass
class FillState:
    """What the human is shown before each decision."""

    result: FilledForm | None
    error: str | None
    can_propose: bool


AskDecision = Callable[[Application, Job, FillState], Awaitable[Decision]]
AskStale = Callable[[Application, Job], Awaitable[StaleDecision]]
Say = Callable[[str], None]


class ContextProvider(Protocol):
    async def for_job(self, job: Job) -> tuple[BrowserContext, PlatformAdapter | None]: ...


def default_adapters(preferences: UserPreferences) -> dict[JobSource, PlatformAdapter]:
    return {p: cls(preferences.platform(p.value)) for p, cls in all_adapter_classes().items()}


class BrowserPool:
    """Browsers for one apply session, opened lazily and closed together.

    Jobs on a platform whose adapter drives a browser use that platform's
    persistent (logged-in) profile; everything else shares one fresh,
    non-persistent browser so employer sites don't pile up cookies.
    """

    def __init__(
        self,
        profiles: BrowserProfiles,
        adapters: dict[JobSource, PlatformAdapter],
        *,
        headless: bool = False,
    ) -> None:
        self._profiles = profiles
        self._adapters = adapters
        self._headless = headless
        self._stack = AsyncExitStack()
        self._contexts: dict[JobSource, BrowserContext] = {}
        self._generic: BrowserContext | None = None

    async def __aenter__(self) -> BrowserPool:
        await self._stack.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._stack.__aexit__(exc_type, exc, tb)

    async def for_job(self, job: Job) -> tuple[BrowserContext, PlatformAdapter | None]:
        adapter = self._adapters.get(job.source)
        if adapter is not None and adapter.uses_browser:
            if job.source not in self._contexts:
                self._contexts[job.source] = await self._stack.enter_async_context(
                    self._profiles.open(job.source, headless=self._headless)
                )
            return self._contexts[job.source], adapter
        if self._generic is None:
            playwright = await self._stack.enter_async_context(async_playwright())
            browser = await playwright.chromium.launch(headless=self._headless)
            self._stack.push_async_callback(browser.close)
            self._generic = await browser.new_context(no_viewport=not self._headless)
        return self._generic, None


class ApplyRunner:
    def __init__(
        self,
        service: ApplicationService,
        engine: AutofillEngine,
        browsers: ContextProvider,
        *,
        ask: AskDecision,
        say: Say,
        proposer: AnswerProposer | None = None,
        paused: Callable[[], bool] = lambda: False,
    ) -> None:
        self._service = service
        self._engine = engine
        self._browsers = browsers
        self._ask = ask
        self._say = say
        self._proposer = proposer
        self._paused = paused

    async def resolve_stale(
        self, stale: list[tuple[Application, Job]], ask: AskStale
    ) -> list[ApplyOutcome]:
        """Settle applications left open by a session that crashed or was killed.

        Only the human knows whether they clicked submit before it died, so ask;
        never guess, and never reopen it automatically (that risks a duplicate).
        """
        outcomes = []
        for application, job in stale:
            assert application.id is not None
            answer = await ask(application, job)
            if answer is StaleDecision.SUBMITTED:
                updated = await self._service.mark_submitted(application.id)
            elif answer is StaleDecision.REJECT:
                updated = await self._service.reject(application.id)
            else:
                updated = await self._service.release(
                    application.id, notes="Previous apply session ended before confirmation"
                )
            outcomes.append(ApplyOutcome(application.id, updated.status, "resolved"))
        return outcomes

    async def process(self, application_id: int) -> ApplyOutcome:
        if self._paused():
            return ApplyOutcome(application_id, None, "paused", stop=True)
        try:
            application, job = await self._service.claim_for_autofill(application_id)
        except DailyCapReachedError as exc:
            # A per-platform cap only skips that platform's jobs; the global cap stops.
            return ApplyOutcome(application_id, None, str(exc), stop=exc.platform is None)
        except InvalidTransitionError as exc:
            return ApplyOutcome(application_id, exc.current, f"skipped: {exc}")

        try:
            context, adapter = await self._browsers.for_job(job)
            tabs_before = set(context.pages)
            page = await context.new_page()
        except Exception as exc:  # noqa: BLE001 - e.g. the profile is open in another window
            released = await self._service.release(application_id, notes=f"Couldn't open: {exc}")
            return ApplyOutcome(application_id, released.status, str(exc), stop=True)

        try:
            form_page, error = await self._open(page, job, adapter)
            if form_page is None:  # the posting is gone / unreachable
                failed = await self._service.mark_failed(application_id, reason=error or "")
                return ApplyOutcome(application_id, failed.status, error or "failed")
            result, fill_error = await self._fill(form_page, application)
            error = error or fill_error
            while True:
                state = FillState(
                    result=result,
                    error=error,
                    can_propose=bool(self._proposer and result and result.fields_skipped),
                )
                decision = await self._ask(application, job, state)
                if decision is Decision.REFILL:
                    if adapter is not None and error and _is_wall(error):
                        # The human logged in / cleared the check: reopen the form.
                        reopened, error = await self._open(form_page, job, adapter)
                        form_page = reopened or form_page
                    form_page = _newest_page(context, form_page)
                    result, fill_error = await self._fill(form_page, application)
                    error = error if error and _is_wall(error) else fill_error
                    continue
                if decision is Decision.PROPOSE and state.can_propose and result is not None:
                    return await self._propose(application, job, result)
                return await self._finish(application_id, decision)
        finally:
            # Close this application's tabs, including any the site opened.
            for tab in [p for p in context.pages if p not in tabs_before]:
                with contextlib.suppress(PlaywrightError):  # the human may have closed it
                    await tab.close()

    async def _open(
        self, page: Page, job: Job, adapter: PlatformAdapter | None
    ) -> tuple[Page | None, str | None]:
        """Open the posting's application. Returns (page with the form, message).

        (None, reason) means the posting can't be reached at all (removed, 404).
        Walls and missing Apply buttons keep the page open for the human.
        """
        if adapter is None:
            try:
                await self._engine.open(page, job.application_url)
            except AutofillError as exc:
                return None, str(exc)
            return page, None
        try:
            return await adapter.open_application(page, job), None
        except SessionExpiredError as exc:
            await self._service.record_platform_session(
                job.source, SessionStatus.NEEDS_LOGIN, str(exc)
            )
            return page, f"{exc} — or log in right here in the browser window, then press f."
        except ChallengeError as exc:
            await self._service.record_platform_session(job.source, SessionStatus.BLOCKED, str(exc))
            return page, f"{exc} Complete it yourself in the browser window, then press f."
        except PlatformError as exc:
            return page, f"{exc} Open the application yourself in the window, then press f."
        except PlaywrightError as exc:
            reason = str(exc).splitlines()[0]
            if "net::" in reason or "Timeout" in reason:
                return None, f"Could not open {job.application_url}: {reason}"
            return page, reason

    async def _fill(
        self, page: Page, application: Application
    ) -> tuple[FilledForm | None, str | None]:
        assert application.id is not None
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        try:
            result = await self._engine.fill_page(
                page,
                cover_letter=application.cover_letter,
                extra_answers=application.answers,
                screenshot_name=f"application_{application.id}_{stamp}.png",
            )
        except AutofillError as exc:
            logger.warning("Autofill failed for application %s: %s", application.id, exc)
            return None, str(exc)
        await self._service.record_autofill(
            application.id, screenshot_path=result.screenshot_path, summary=result.summary()
        )
        return result, None

    async def _propose(
        self, application: Application, job: Job, result: FilledForm
    ) -> ApplyOutcome:
        assert application.id is not None and self._proposer is not None
        try:
            proposed = await self._proposer.propose(job, result.fields_skipped)
        except Exception as exc:  # noqa: BLE001 - the human can still answer by hand
            released = await self._service.release(
                application.id, notes=f"Couldn't draft answers ({exc}); answer them yourself."
            )
            return ApplyOutcome(application.id, released.status, f"LLM failed: {exc}")
        if not proposed:
            released = await self._service.release(
                application.id,
                notes="JobPilot couldn't honestly answer the unknown questions; answer "
                "them yourself in the browser.",
            )
            return ApplyOutcome(application.id, released.status, "no answers proposed")
        updated = await self._service.propose_answers(application.id, proposed)
        return ApplyOutcome(
            application.id, updated.status, f"{len(proposed)} proposed answer(s) sent to review"
        )

    async def _finish(self, application_id: int, decision: Decision) -> ApplyOutcome:
        if decision is Decision.SUBMITTED:
            updated = await self._service.mark_submitted(application_id)
            return ApplyOutcome(application_id, updated.status, "submitted (confirmed by you)")
        if decision is Decision.REJECT:
            updated = await self._service.reject(application_id)
            return ApplyOutcome(application_id, updated.status, "rejected")
        updated = await self._service.release(
            application_id, notes="Opened and autofilled, not submitted yet"
        )
        return ApplyOutcome(
            application_id, updated.status, "kept for later", stop=decision is Decision.QUIT
        )


def _is_wall(message: str) -> bool:
    return "login required" in message or "won't bypass" in message


def _newest_page(context: BrowserContext, current: Page) -> Page:
    """Follow the human: if they opened the employer's form in a new tab, fill that."""
    open_pages = [p for p in context.pages if not p.is_closed()]
    if open_pages and (current.is_closed() or open_pages[-1] is not current):
        return open_pages[-1]
    return current
