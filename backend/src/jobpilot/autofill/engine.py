"""Playwright-based form autofill engine.

Heuristically detects fields by label, name, placeholder, and autocomplete
attributes, then fills them from the applicant profile + generated materials.

Key safety invariant: this engine NEVER clicks submit. It fills the form and
takes a screenshot; actual submission is gated by the approval workflow.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from playwright.async_api import Locator, Page, async_playwright

from jobpilot.config.preferences import ApplicantProfile

logger = logging.getLogger(__name__)

# Maps (lowered pattern → profile field). Checked against label text, name attr,
# placeholder, and autocomplete. First match wins, so more-specific patterns
# go first.
_FIELD_MAP: list[tuple[list[str], str]] = [
    (["full name", "your name", "first name", "last name", "name"], "name"),
    (["email", "e-mail"], "email"),
    (["phone", "mobile", "telephone"], "phone"),
    (["address", "city", "street"], "address"),
    (["github"], "github_url"),
    (["linkedin"], "linkedin_url"),
    (["portfolio", "website", "personal site"], "portfolio_url"),
    (
        ["work authorization", "authorized to work", "visa", "sponsorship", "legally authorized"],
        "work_authorization",
    ),
    (
        ["salary", "compensation", "expected salary", "desired salary", "pay expectation"],
        "salary_expectation",
    ),
    (["start date", "available", "earliest start", "when can you start"], "available_start_date"),
    (["years of experience", "years experience", "yrs experience"], "years_experience"),
]

_COVER_LETTER_PATTERNS = [
    "cover letter",
    "cover_letter",
    "coverletter",
    "why do you want to work",
    "why are you interested",
    "tell us about yourself",
    "introduction",
]

_RESUME_PATTERNS = ["resume", "cv", "curriculum"]


def _text_matches(text: str, patterns: list[str]) -> bool:
    lowered = text.lower()
    return any(p in lowered for p in patterns)


def _profile_value(applicant: ApplicantProfile, field_name: str) -> str:
    return str(getattr(applicant, field_name, ""))


@dataclass
class FilledForm:
    """Result of an autofill run — what was filled, what was skipped, screenshot path."""

    url: str
    fields_filled: dict[str, str] = field(default_factory=dict)
    fields_skipped: list[str] = field(default_factory=list)
    resume_uploaded: bool = False
    cover_letter_filled: bool = False
    screenshot_path: str | None = None


class AutofillEngine:
    """Fill a job application form via Playwright (headless Chromium).

    Usage::

        engine = AutofillEngine(applicant_profile, screenshots_dir)
        result = await engine.fill(url, cover_letter="...", extra_answers={...})
    """

    def __init__(
        self,
        applicant: ApplicantProfile,
        screenshots_dir: Path | None = None,
        *,
        headless: bool = True,
    ) -> None:
        self._applicant = applicant
        self._screenshots_dir = screenshots_dir
        self._headless = headless

    async def fill(
        self,
        url: str,
        *,
        cover_letter: str = "",
        extra_answers: dict[str, str] | None = None,
    ) -> FilledForm:
        result = FilledForm(url=url)

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self._headless)
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                locale="en-US",
            )
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(2000)

            await self._fill_text_fields(page, result)

            if cover_letter:
                await self._fill_cover_letter(page, cover_letter, result)

            if extra_answers:
                await self._fill_extra_answers(page, extra_answers, result)

            await self._upload_resume(page, result)

            if self._screenshots_dir:
                self._screenshots_dir.mkdir(parents=True, exist_ok=True)
                ss_path = self._screenshots_dir / f"autofill_{page.url.split('/')[-1][:40]}.png"
                await page.screenshot(path=str(ss_path), full_page=True)
                result.screenshot_path = str(ss_path)
                logger.info("Screenshot saved: %s", ss_path)

            await browser.close()

        return result

    async def _fill_text_fields(self, page: Page, result: FilledForm) -> None:
        inputs = page.locator("input[type='text'], input[type='email'], input[type='tel'], "
                              "input[type='url'], input[type='number'], input:not([type])")
        count = await inputs.count()

        for i in range(count):
            inp = inputs.nth(i)
            if not await inp.is_visible():
                continue

            identity = await self._identify_field(inp, page)
            if identity is None:
                label_text = await self._get_label_text(inp, page)
                if label_text:
                    result.fields_skipped.append(label_text)
                continue

            value = _profile_value(self._applicant, identity)
            if not value:
                continue

            try:
                await inp.fill(value)
                result.fields_filled[identity] = value
                logger.debug("Filled %s = %s", identity, value[:30])
            except Exception:  # noqa: BLE001
                logger.warning("Failed to fill field %s", identity)

        # Also handle textarea fields (that aren't cover-letter-sized)
        textareas = page.locator("textarea")
        ta_count = await textareas.count()
        for i in range(ta_count):
            ta = textareas.nth(i)
            if not await ta.is_visible():
                continue
            identity = await self._identify_field(ta, page)
            if identity and _profile_value(self._applicant, identity):
                try:
                    await ta.fill(_profile_value(self._applicant, identity))
                    result.fields_filled[identity] = _profile_value(self._applicant, identity)
                except Exception:  # noqa: BLE001
                    pass

    async def _identify_field(self, locator: Locator, page: Page) -> str | None:
        signals: list[str] = []

        for attr in ("name", "id", "placeholder", "autocomplete", "aria-label"):
            val = await locator.get_attribute(attr)
            if val:
                signals.append(val)

        label_text = await self._get_label_text(locator, page)
        if label_text:
            signals.append(label_text)

        combined = " ".join(signals).lower()
        for patterns, field_name in _FIELD_MAP:
            if any(p in combined for p in patterns):
                return field_name
        return None

    async def _get_label_text(self, locator: Locator, page: Page) -> str:
        field_id = await locator.get_attribute("id")
        if field_id:
            label = page.locator(f"label[for='{field_id}']")
            if await label.count() > 0:
                text = await label.first.text_content()
                if text:
                    return text.strip()

        parent = locator.locator("xpath=ancestor::label")
        if await parent.count() > 0:
            text = await parent.first.text_content()
            if text:
                return text.strip()

        return ""

    async def _fill_cover_letter(self, page: Page, cover_letter: str, result: FilledForm) -> None:
        textareas = page.locator("textarea")
        count = await textareas.count()
        for i in range(count):
            ta = textareas.nth(i)
            if not await ta.is_visible():
                continue

            signals: list[str] = []
            for attr in ("name", "id", "placeholder", "aria-label"):
                val = await ta.get_attribute(attr)
                if val:
                    signals.append(val)
            label_text = await self._get_label_text(ta, page)
            if label_text:
                signals.append(label_text)

            combined = " ".join(signals).lower()
            if _text_matches(combined, _COVER_LETTER_PATTERNS):
                try:
                    await ta.fill(cover_letter)
                    result.cover_letter_filled = True
                    result.fields_filled["cover_letter"] = cover_letter[:50] + "…"
                    logger.debug("Cover letter filled")
                    return
                except Exception:  # noqa: BLE001
                    logger.warning("Failed to fill cover letter textarea")

    async def _fill_extra_answers(
        self, page: Page, answers: dict[str, str], result: FilledForm
    ) -> None:
        """Fill additional text fields matched by question text in their labels."""
        for question, answer in answers.items():
            labels = page.locator("label")
            label_count = await labels.count()
            for i in range(label_count):
                label = labels.nth(i)
                label_text = await label.text_content()
                if not label_text or question.lower() not in label_text.lower():
                    continue
                field_id = await label.get_attribute("for")
                if field_id:
                    field = page.locator(f"#{field_id}")
                    if await field.count() > 0 and await field.first.is_visible():
                        try:
                            await field.first.fill(answer)
                            result.fields_filled[f"q:{question[:30]}"] = answer[:50]
                        except Exception:  # noqa: BLE001
                            pass

    async def _upload_resume(self, page: Page, result: FilledForm) -> None:
        resume_path = self._applicant.resume_file
        if not resume_path or not Path(resume_path).exists():
            return

        file_inputs = page.locator("input[type='file']")
        count = await file_inputs.count()
        for i in range(count):
            inp = file_inputs.nth(i)
            signals: list[str] = []
            for attr in ("name", "id", "accept", "aria-label"):
                val = await inp.get_attribute(attr)
                if val:
                    signals.append(val)
            label_text = await self._get_label_text(inp, page)
            if label_text:
                signals.append(label_text)

            combined = " ".join(signals).lower()
            if _text_matches(combined, _RESUME_PATTERNS) or ".pdf" in combined:
                try:
                    await inp.set_input_files(resume_path)
                    result.resume_uploaded = True
                    result.fields_filled["resume_upload"] = Path(resume_path).name
                    logger.info("Resume uploaded: %s", resume_path)
                    return
                except Exception:  # noqa: BLE001
                    logger.warning("Failed to upload resume")
