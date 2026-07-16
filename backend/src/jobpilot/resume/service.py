"""Resume import orchestration: PDF -> text -> profile -> database."""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.database.repositories import ResumeRepository
from jobpilot.domain.models import Resume
from jobpilot.llm.provider import LLMProvider
from jobpilot.resume.extract import extract_text_from_pdf
from jobpilot.resume.parse import parse_resume_text

logger = logging.getLogger(__name__)


async def import_resume(
    session_factory: async_sessionmaker[AsyncSession],
    path: Path,
    *,
    version: str = "default",
    activate: bool = True,
    llm: LLMProvider | None = None,
) -> Resume:
    """Parse and store (or re-import) a resume version."""
    text = extract_text_from_pdf(path)
    profile = await parse_resume_text(text, llm)

    async with session_factory() as session:
        repo = ResumeRepository(session)
        existing = await repo.get_by_version(version)
        if existing is not None:
            raise ValueError(
                f"Resume version {version!r} already exists (id={existing.id}). "
                "Pick a new --version name."
            )
        resume = await repo.add(
            Resume(version=version, file_path=str(path.resolve()), profile=profile)
        )
        if activate:
            assert resume.id is not None
            await repo.set_active(resume.id)
            resume = await repo.get(resume.id) or resume
        await session.commit()
        logger.info(
            "Imported resume %s (version=%s, %d technologies, %.1f yrs)",
            path.name,
            version,
            len(profile.technologies),
            profile.years_experience or 0.0,
        )
        return resume
