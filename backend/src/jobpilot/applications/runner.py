"""`jobpilot apply`: open approved applications in a visible browser and wait for the human.

Flow per application:
    APPROVED --claim--> AWAITING_CONFIRMATION --(human submits in the browser,
    then confirms here)--> SUBMITTED

JobPilot opens the page and autofills what it can identify. The human checks
every field, handles anything JobPilot flagged (unknown questions, sensitive
fields, CAPTCHAs, logins), clicks the site's own submit button, and then tells
JobPilot. SUBMITTED is recorded only on that explicit confirmation.

I/O goes through the `ask`/`say` callables so the flow is testable without a
terminal.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from playwright.async_api import BrowserContext, Page
from playwright.async_api import Error as PlaywrightError

from jobpilot.applications.service import ApplicationService
from jobpilot.autofill.engine import AutofillEngine, AutofillError, FilledForm
from jobpilot.database.repositories import DailyCapReachedError, InvalidTransitionError
from jobpilot.domain.enums import ApplicationStatus
from jobpilot.domain.models import Application, Job

logger = logging.getLogger(__name__)


class Decision(StrEnum):
    SUBMITTED = "submitted"  # the human submitted it on the site
    REFILL = "refill"  # fill the current page again (e.g. after clicking "Next")
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


AskDecision = Callable[[Application, Job, FilledForm | None, str | None], Awaitable[Decision]]
AskStale = Callable[[Application, Job], Awaitable[StaleDecision]]
Say = Callable[[str], None]


class ApplyRunner:
    def __init__(
        self,
        service: ApplicationService,
        engine: AutofillEngine,
        *,
        ask: AskDecision,
        say: Say,
    ) -> None:
        self._service = service
        self._engine = engine
        self._ask = ask
        self._say = say

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

    async def process(self, context: BrowserContext, application_id: int) -> ApplyOutcome:
        try:
            application, job = await self._service.claim_for_autofill(application_id)
        except DailyCapReachedError as exc:
            return ApplyOutcome(application_id, None, str(exc), stop=True)
        except InvalidTransitionError as exc:
            return ApplyOutcome(application_id, exc.current, f"skipped: {exc}")

        page = await context.new_page()
        try:
            try:
                await self._engine.open(page, job.application_url)
            except AutofillError as exc:
                failed = await self._service.mark_failed(application_id, reason=str(exc))
                return ApplyOutcome(application_id, failed.status, str(exc))

            result, error = await self._fill(page, application)
            while True:
                decision = await self._ask(application, job, result, error)
                if decision is Decision.REFILL:
                    result, error = await self._fill(page, application)
                    continue
                return await self._finish(application_id, decision)
        finally:
            with contextlib.suppress(PlaywrightError):  # the human may have closed the tab
                await page.close()

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
            application_id,
            updated.status,
            "kept for later",
            stop=decision is Decision.QUIT,
        )
