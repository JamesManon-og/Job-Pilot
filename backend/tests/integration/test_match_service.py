from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from jobpilot.config.preferences import UserPreferences
from jobpilot.database import create_session_factory
from jobpilot.database.repositories import JobRepository, ResumeRepository
from jobpilot.domain import Job, JobSource, MatchRecommendation, Resume, ResumeProfile
from jobpilot.matcher import MatchEngine, MatchService, NoActiveResumeError


class FakeLLM:
    """Returns a canned score derived from the prompt so tests are deterministic."""

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = payload or {
            "score": 88,
            "matched_skills": ["react", "typescript"],
            "missing_skills": ["kubernetes"],
            "recommendation": "strong_apply",
            "reasoning": "Strong frontend overlap.",
        }
        self.prompts: list[str] = []

    async def generate(self, prompt: str, *, system: str | None = None) -> str:
        return json.dumps(self.payload)

    async def generate_json(
        self, prompt: str, *, system: str | None = None, schema: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.prompts.append(prompt)
        return self.payload


async def seed(engine: AsyncEngine, *, jobs: int = 2) -> Any:
    factory = create_session_factory(engine)
    async with factory() as session:
        repo = JobRepository(session)
        for i in range(1, jobs + 1):
            await repo.upsert(
                Job(
                    title=f"Frontend Engineer {i}",
                    company=f"Acme{i}",
                    application_url=f"https://a.com/{i}",
                    source=JobSource.REMOTEOK,
                    technologies=["react"],
                )
            )
        resume = await ResumeRepository(session).add(
            Resume(
                version="v1",
                file_path="/r/v1.pdf",
                profile=ResumeProfile(technologies=["react", "typescript"]),
                is_active=True,
            )
        )
        await session.commit()
    return factory, resume


def make_service(factory: Any, llm: FakeLLM | None = None) -> MatchService:
    return MatchService(
        factory,
        MatchEngine(llm or FakeLLM(), model_name="fake"),
        UserPreferences(),
    )


class TestMatchService:
    async def test_match_unscored_scores_everything_once(self, engine: AsyncEngine) -> None:
        factory, _ = await seed(engine, jobs=3)
        service = make_service(factory)

        results = await service.match_unscored()
        assert len(results) == 3
        assert all(r.score == 88 for r in results)
        assert all(r.recommendation is MatchRecommendation.STRONG_APPLY for r in results)

        # Re-run: nothing left to score.
        assert await service.match_unscored() == []

    async def test_match_requires_active_resume(self, engine: AsyncEngine) -> None:
        factory = create_session_factory(engine)
        service = make_service(factory)
        with pytest.raises(NoActiveResumeError):
            await service.match_unscored()

    async def test_prompt_includes_job_and_profile(self, engine: AsyncEngine) -> None:
        factory, _ = await seed(engine, jobs=1)
        llm = FakeLLM()
        service = make_service(factory, llm)
        await service.match_unscored()
        prompt = llm.prompts[0]
        assert "Frontend Engineer 1" in prompt
        assert "react" in prompt

    async def test_ranked_orders_by_composite(self, engine: AsyncEngine) -> None:
        factory, _ = await seed(engine, jobs=2)
        service = make_service(factory)
        await service.match_unscored()
        ranked = await service.ranked(top=10)
        assert len(ranked) == 2
        assert ranked[0].composite_score >= ranked[1].composite_score

    async def test_bad_llm_values_are_coerced(self, engine: AsyncEngine) -> None:
        factory, _ = await seed(engine, jobs=1)
        llm = FakeLLM(
            {
                "score": "not a number",
                "matched_skills": None,
                "missing_skills": ["x"],
                "recommendation": "definitely!!",
                "reasoning": None,
            }
        )
        service = make_service(factory, llm)
        results = await service.match_unscored()
        assert results[0].score == 0
        assert results[0].recommendation is MatchRecommendation.SKIP
