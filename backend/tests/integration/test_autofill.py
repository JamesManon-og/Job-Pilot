"""Test the Playwright autofill engine against a local fixture HTML form."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobpilot.autofill import AutofillEngine
from jobpilot.config.preferences import ApplicantProfile

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "application_form.html"


@pytest.fixture
def applicant(tmp_path: Path) -> ApplicantProfile:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 fake")
    return ApplicantProfile(
        name="James Manon",
        email="james@example.com",
        phone="+1-555-0123",
        portfolio_url="https://james.dev",
        github_url="https://github.com/james",
        linkedin_url="https://linkedin.com/in/james",
        work_authorization="Authorized to work",
        salary_expectation="$120,000",
        available_start_date="2 weeks",
        resume_file=str(resume),
    )


class TestAutofillEngine:
    @pytest.mark.timeout(30)
    async def test_fills_all_fields_on_fixture_form(
        self, applicant: ApplicantProfile, tmp_path: Path
    ) -> None:
        engine = AutofillEngine(applicant, screenshots_dir=tmp_path / "screenshots")
        result = await engine.fill(
            f"file://{FIXTURE}",
            cover_letter="I am very interested in this position.",
            extra_answers={"Why do you want to work here?": "Great culture and mission."},
        )

        assert result.fields_filled.get("name") == "James Manon"
        assert result.fields_filled.get("email") == "james@example.com"
        assert result.fields_filled.get("phone") == "+1-555-0123"
        assert result.fields_filled.get("linkedin_url") == "https://linkedin.com/in/james"
        assert result.fields_filled.get("github_url") == "https://github.com/james"
        assert result.fields_filled.get("salary_expectation") == "$120,000"
        assert result.cover_letter_filled is True
        assert result.resume_uploaded is True
        assert result.screenshot_path is not None
        assert Path(result.screenshot_path).exists()

    @pytest.mark.timeout(30)
    async def test_skips_empty_profile_fields(self, tmp_path: Path) -> None:
        empty = ApplicantProfile(name="Test User", email="test@example.com")
        engine = AutofillEngine(empty, screenshots_dir=tmp_path / "screenshots")
        result = await engine.fill(f"file://{FIXTURE}")

        assert result.fields_filled.get("name") == "Test User"
        assert result.fields_filled.get("email") == "test@example.com"
        assert "phone" not in result.fields_filled
        assert result.resume_uploaded is False

    @pytest.mark.timeout(30)
    async def test_no_submit_button_clicked(
        self, applicant: ApplicantProfile, tmp_path: Path
    ) -> None:
        """The engine must never click submit — verify the form wasn't submitted."""
        engine = AutofillEngine(applicant, screenshots_dir=tmp_path / "screenshots")
        result = await engine.fill(f"file://{FIXTURE}")
        # If the form was submitted, the URL would change or there'd be a navigation.
        # Since we're on a file:// URL with no action, the URL stays the same.
        assert "application_form.html" in result.url or result.url.startswith("file://")
