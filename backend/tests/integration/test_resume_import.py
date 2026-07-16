from __future__ import annotations

from pathlib import Path

import pytest
from fpdf import FPDF
from sqlalchemy.ext.asyncio import AsyncEngine

from jobpilot.database import create_session_factory
from jobpilot.resume import extract_text_from_pdf, import_resume


@pytest.fixture
def sample_pdf(tmp_path: Path) -> Path:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    lines = [
        "Jane Doe",
        "Full Stack Developer - 3 years of experience",
        "",
        "Skills",
        "React, TypeScript, Node.js, PostgreSQL",
        "",
        "Education",
        "BS Computer Science",
    ]
    for line in lines:
        pdf.cell(0, 8, line, new_x="LMARGIN", new_y="NEXT")
    path = tmp_path / "resume.pdf"
    pdf.output(str(path))
    return path


class TestResumeImport:
    def test_extract_text(self, sample_pdf: Path) -> None:
        text = extract_text_from_pdf(sample_pdf)
        assert "React" in text and "PostgreSQL" in text

    def test_extract_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            extract_text_from_pdf(tmp_path / "nope.pdf")

    async def test_import_end_to_end(self, engine: AsyncEngine, sample_pdf: Path) -> None:
        factory = create_session_factory(engine)
        resume = await import_resume(factory, sample_pdf, version="v1")
        assert resume.id is not None
        assert resume.is_active is True
        assert "react" in resume.profile.technologies
        assert resume.profile.years_experience == 3.0

    async def test_duplicate_version_rejected(self, engine: AsyncEngine, sample_pdf: Path) -> None:
        factory = create_session_factory(engine)
        await import_resume(factory, sample_pdf, version="v1")
        with pytest.raises(ValueError, match="already exists"):
            await import_resume(factory, sample_pdf, version="v1")
