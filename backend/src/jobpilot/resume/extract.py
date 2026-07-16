"""PDF text extraction."""

from __future__ import annotations

from pathlib import Path

import pdfplumber


def extract_text_from_pdf(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Resume file not found: {path}")
    pages: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
    text = "\n".join(pages).strip()
    if not text:
        raise ValueError(
            f"No extractable text in {path.name} — is it a scanned image? "
            "Export the resume as a text-based PDF."
        )
    return text
