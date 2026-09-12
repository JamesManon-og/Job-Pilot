"""Autofill safety and robustness against realistic, hostile form markup.

Each class maps to a failure mode from the reliability audit: guessing
ambiguous fields, crashing on odd ids, uploading the resume into the wrong
slot, overwriting prefilled data, touching sensitive/consent fields, and
interacting with login/MFA/bot walls.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from playwright.async_api import BrowserContext, Page, Route, async_playwright

from jobpilot.autofill import AutofillEngine, AutofillError
from jobpilot.config.preferences import ApplicantProfile

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def applicant(tmp_path: Path) -> ApplicantProfile:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 fake")
    return ApplicantProfile(
        name="James Manon",
        email="james@example.com",
        phone="+63 900 000 0000",
        linkedin_url="linkedin.com/in/james",
        work_authorization="Yes",
        requires_sponsorship="No",
        years_experience="3",
        resume_file=str(resume),
    )


@pytest.fixture
async def context() -> AsyncIterator[BrowserContext]:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context()
        yield ctx
        await browser.close()


async def value(page: Page, selector: str) -> str:
    return await page.locator(selector).input_value()


ANSWERS = {
    "Why do you want to work here?": "Your mission.",
    "Why are you qualified for this role?": "Five shipped React apps.",
}


class TestTrickyForm:
    @pytest.fixture
    async def filled(
        self, context: BrowserContext, applicant: ApplicantProfile, tmp_path: Path
    ) -> tuple[Page, object]:
        page = await context.new_page()
        await page.goto(f"file://{FIXTURES / 'tricky_form.html'}")
        engine = AutofillEngine(applicant, tmp_path / "shots")
        result = await engine.fill_page(page, cover_letter="Letter", extra_answers=ANSWERS)
        return page, result

    async def test_ambiguous_name_fields_are_not_guessed(self, filled: tuple[Page, object]) -> None:
        """Bug: any field containing "name" got the applicant's full name."""
        page, _ = filled
        assert await value(page, "#company_name") == ""
        assert await value(page, "#username") == ""
        assert await value(page, "#ref_name") == ""

    async def test_first_and_last_name_are_split(self, filled: tuple[Page, object]) -> None:
        page, _ = filled
        assert await value(page, "#fn") == "James"
        assert await value(page, "#ln") == "Manon"

    async def test_prefilled_values_are_not_overwritten(self, filled: tuple[Page, object]) -> None:
        page, _ = filled
        assert await value(page, "#em") == "prefilled@platform.test"

    async def test_label_without_for_attribute(self, filled: tuple[Page, object]) -> None:
        page, _ = filled
        assert await value(page, "[name=q_12]") == "+63 900 000 0000"

    async def test_ids_that_are_invalid_css_do_not_crash(self, filled: tuple[Page, object]) -> None:
        """Bug: page.locator(f"#{id}") raised on ids like "1st-question"."""
        page, _ = filled
        assert await page.locator('[id="1st-question"]').input_value() == "Your mission."
        assert await page.locator('[id="q[2]\'x"]').input_value() == "Five shipped React apps."

    async def test_resume_goes_into_the_resume_input_not_the_cover_letter_one(
        self, filled: tuple[Page, object]
    ) -> None:
        """Bug: any file input accepting .pdf got the resume — first match won."""
        page, result = filled
        cl = await page.locator("#cl_file").evaluate("el => el.files.length")
        cv = await page.locator("#cv_file").evaluate("el => el.files.length")
        assert (cl, cv) == (0, 1)
        assert result.resume_uploaded is True  # type: ignore[attr-defined]

    async def test_select_and_radio_answered_from_profile(
        self, filled: tuple[Page, object]
    ) -> None:
        page, _ = filled
        assert await value(page, "#auth") == "y"
        assert await page.locator("input[name=sponsor][value=no]").is_checked()
        assert not await page.locator("input[name=sponsor][value=yes]").is_checked()

    async def test_sensitive_fields_are_left_alone_and_reported(
        self, filled: tuple[Page, object]
    ) -> None:
        page, result = filled
        for selector in ("#dob", "#ssn", "#cur_sal", "#gender"):
            assert await value(page, selector) == ""
        sensitive = result.sensitive_skipped  # type: ignore[attr-defined]
        assert {"Date of birth", "Gender", "SSN", "Current salary"} <= set(sensitive)

    async def test_consent_checkbox_is_never_ticked(self, filled: tuple[Page, object]) -> None:
        page, result = filled
        assert not await page.locator("#consent").is_checked()
        assert "I agree to the privacy policy" in result.required_unfilled  # type: ignore[attr-defined]

    async def test_unknown_required_question_is_reported_not_guessed(
        self, filled: tuple[Page, object]
    ) -> None:
        page, result = filled
        assert await value(page, "#why_rust") == ""
        assert "Describe your experience with Rust" in result.fields_skipped  # type: ignore[attr-defined]
        assert "Describe your experience with Rust" in result.required_unfilled  # type: ignore[attr-defined]

    async def test_technology_specific_years_are_not_given_the_generic_number(
        self, filled: tuple[Page, object]
    ) -> None:
        page, _ = filled
        assert await value(page, "#yrs_react") == ""
        assert await value(page, "#yrs") == "3"

    async def test_fields_revealed_after_typing_are_filled(
        self, filled: tuple[Page, object]
    ) -> None:
        page, _ = filled
        assert await value(page, "#linkedin_dyn") == "https://linkedin.com/in/james"

    async def test_form_is_never_submitted(self, filled: tuple[Page, object]) -> None:
        """The old test only checked the URL, which doesn't change for this form
        even when it IS submitted. Check the submit handler instead."""
        page, _ = filled
        assert await page.evaluate("window.__submitted === true") is False

    async def test_no_pii_in_audit_summary(self, filled: tuple[Page, object]) -> None:
        _, result = filled
        summary = str(result.summary())  # type: ignore[attr-defined]
        assert "James" not in summary and "+63" not in summary


BLOCKER_PAGES = {
    "login": """<title>Sign in</title><form><label>Email<input name=email></label>
        <label>Password<input type=password name=pw></label></form>""",
    "mfa": """<title>Verify</title><p>Enter the code we sent you.</p>
        <input autocomplete="one-time-code" name="code">""",
    "challenge": "<title>Just a moment...</title><p>Checking your browser before accessing.</p>",
}


class TestBlockers:
    @pytest.mark.parametrize("kind", sorted(BLOCKER_PAGES))
    async def test_walls_stop_the_fill_without_typing_anything(
        self, context: BrowserContext, applicant: ApplicantProfile, kind: str
    ) -> None:
        page = await context.new_page()
        await page.set_content(BLOCKER_PAGES[kind])
        result = await AutofillEngine(applicant).fill_page(page)
        assert result.blocker == kind
        assert result.fields_filled == {}
        values = await page.evaluate(
            "Array.from(document.querySelectorAll('input')).map(i => i.value)"
        )
        assert all(v == "" for v in values)

    async def test_security_words_in_a_job_description_are_not_an_mfa_wall(
        self, context: BrowserContext, applicant: ApplicantProfile
    ) -> None:
        page = await context.new_page()
        await page.goto(f"file://{FIXTURES / 'tricky_form.html'}")
        result = await AutofillEngine(applicant).fill_page(page)
        assert result.blocker is None

    async def test_captcha_widget_is_reported_and_left_for_the_human(
        self, context: BrowserContext, applicant: ApplicantProfile
    ) -> None:
        page = await context.new_page()

        async def blank(route: Route) -> None:
            await route.fulfill(status=200, body="<html></html>", content_type="text/html")

        await page.route("**/recaptcha/**", blank)
        await page.set_content(
            """<label for=n>Full name</label><input id=n>
            <iframe title="reCAPTCHA" width=300 height=80
              src="https://www.google.com/recaptcha/api2/anchor?k=x&size=normal"></iframe>"""
        )
        result = await AutofillEngine(applicant).fill_page(page)
        assert result.captcha_present is True
        assert result.blocker is None
        assert await value(page, "#n") == "James Manon"


class TestNavigationFailures:
    async def test_http_404_is_a_clear_error(
        self, context: BrowserContext, applicant: ApplicantProfile
    ) -> None:
        """A job that disappeared from the site."""
        page = await context.new_page()

        async def gone(route: Route) -> None:
            await route.fulfill(status=404, body="Not found")

        await page.route("https://jobs.test/**", gone)
        with pytest.raises(AutofillError, match="404.*removed"):
            await AutofillEngine(applicant).open(page, "https://jobs.test/apply/1")

    async def test_connection_failure_is_a_clear_error(
        self, context: BrowserContext, applicant: ApplicantProfile
    ) -> None:
        page = await context.new_page()
        with pytest.raises(AutofillError, match="Could not open"):
            await AutofillEngine(applicant).open(page, "http://127.0.0.1:9/apply")

    async def test_timeout_is_a_clear_error(
        self, context: BrowserContext, applicant: ApplicantProfile
    ) -> None:
        page = await context.new_page()

        async def hang(route: Route) -> None:
            pass  # never respond

        await page.route("https://slow.test/**", hang)
        with pytest.raises(AutofillError, match="Timed out"):
            await AutofillEngine(applicant).open(page, "https://slow.test/x", timeout_ms=500)


class TestScreenshots:
    async def test_same_url_tail_does_not_overwrite_screenshots(
        self, context: BrowserContext, applicant: ApplicantProfile, tmp_path: Path
    ) -> None:
        """Bug: screenshots were named after the URL's last segment, so every
        ".../apply" page overwrote the previous one."""
        engine = AutofillEngine(applicant, tmp_path)
        paths = []
        for host in ("a.test", "b.test"):
            page = await context.new_page()

            async def form(route: Route) -> None:
                await route.fulfill(body="<input>", content_type="text/html")

            await page.route(f"https://{host}/**", form)
            await engine.open(page, f"https://{host}/apply")
            paths.append((await engine.fill_page(page)).screenshot_path)
        assert paths[0] != paths[1]
        assert all(p and Path(p).exists() for p in paths)


class TestResumeFallback:
    async def test_active_resume_is_uploaded_when_config_has_none(
        self, context: BrowserContext, tmp_path: Path
    ) -> None:
        """Bug: autofill only used applicant.resume_file (empty by default), so
        an imported resume was never attached."""
        resume = tmp_path / "imported.pdf"
        resume.write_bytes(b"%PDF-1.4")
        page = await context.new_page()
        await page.set_content("<label for=r>Resume</label><input type=file id=r>")
        engine = AutofillEngine(ApplicantProfile(name="J"), resume_file=str(resume))
        result = await engine.fill_page(page)
        assert result.resume_uploaded is True

    async def test_missing_resume_file_is_reported(
        self, context: BrowserContext, tmp_path: Path
    ) -> None:
        page = await context.new_page()
        await page.set_content("<label for=r>Resume</label><input type=file id=r>")
        engine = AutofillEngine(ApplicantProfile(resume_file=str(tmp_path / "nope.pdf")))
        result = await engine.fill_page(page)
        assert result.resume_uploaded is False
        assert result.resume_problem and "not found" in result.resume_problem
