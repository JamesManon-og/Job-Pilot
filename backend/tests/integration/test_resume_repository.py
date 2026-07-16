from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.database.repositories import ResumeRepository
from jobpilot.domain import Resume, ResumeProfile


class TestResumeRepository:
    async def test_add_and_roundtrip_profile(self, session: AsyncSession) -> None:
        repo = ResumeRepository(session)
        resume = await repo.add(
            Resume(
                version="v1",
                file_path="/resumes/v1.pdf",
                profile=ResumeProfile(
                    skills=["frontend"],
                    technologies=["react", "nextjs"],
                    years_experience=3.5,
                ),
            )
        )
        assert resume.id is not None
        fetched = await repo.get(resume.id)
        assert fetched is not None
        assert fetched.profile.technologies == ["react", "nextjs"]
        assert fetched.profile.years_experience == 3.5

    async def test_set_active_deactivates_others(self, session: AsyncSession) -> None:
        repo = ResumeRepository(session)
        first = await repo.add(Resume(version="v1", file_path="/r/v1.pdf", is_active=True))
        second = await repo.add(Resume(version="v2", file_path="/r/v2.pdf"))
        assert first.id is not None and second.id is not None

        await repo.set_active(second.id)
        active = await repo.get_active()
        assert active is not None
        assert active.id == second.id

    async def test_get_by_version(self, session: AsyncSession) -> None:
        repo = ResumeRepository(session)
        await repo.add(Resume(version="v1", file_path="/r/v1.pdf"))
        found = await repo.get_by_version("v1")
        assert found is not None
        assert await repo.get_by_version("nope") is None
