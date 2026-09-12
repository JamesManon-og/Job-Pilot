from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from jobpilot.config.preferences import UserPreferences
from jobpilot.database import create_session_factory
from jobpilot.database.repositories import JobRepository, ResumeRepository
from jobpilot.domain import Job, JobSource, MatchRecommendation, Resume, ResumeProfile
from jobpilot.llm import OllamaError, OllamaUnavailableError
from jobpilot.matcher import MatchEngine, MatchingAbortedError, MatchService, NoActiveResumeError


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
                "score": "62%",
                "matched_skills": "react, typescript",
                "missing_skills": None,
                "recommendation": "definitely!!",
                "reasoning": None,
            }
        )
        service = make_service(factory, llm)
        results = await service.match_unscored()
        assert results[0].score == 62
        assert results[0].matched_skills == ["react", "typescript"]  # not ["r","e","a",...]
        assert results[0].missing_skills == []
        assert results[0].recommendation is MatchRecommendation.MAYBE  # derived from score
        assert results[0].reasoning == ""

    async def test_missing_score_is_not_recorded_as_a_fake_zero(self, engine: AsyncEngine) -> None:
        """Bug: an unparseable score was saved as 0/"skip", hiding the job forever.
        Now the job stays unscored and is retried on the next run."""
        factory, resume = await seed(engine, jobs=1)
        llm = FakeLLM({"score": "not a number", "recommendation": "skip"})
        service = make_service(factory, llm)
        assert await service.match_unscored() == []
        assert list(service.last_failures) == [1]
        async with factory() as session:
            from jobpilot.database.repositories import MatchResultRepository

            assert await MatchResultRepository(session).unmatched_job_ids(resume.id) == [1]


class FlakyLLM(FakeLLM):
    """Fails on chosen calls to simulate a model choking on one posting, or dying."""

    def __init__(self, fail_on: set[int], error: Exception) -> None:
        super().__init__()
        self.calls = 0
        self.fail_on = fail_on
        self.error = error

    async def generate_json(
        self, prompt: str, *, system: str | None = None, schema: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls += 1
        if self.calls in self.fail_on:
            raise self.error
        return await super().generate_json(prompt, system=system, schema=schema)


class TestMatchingResilience:
    async def test_one_bad_job_does_not_abort_the_batch(self, engine: AsyncEngine) -> None:
        """Bug: a single malformed-JSON reply raised out of match_unscored and
        killed the whole `jobpilot match` / `jobpilot run`."""
        factory, _ = await seed(engine, jobs=3)
        llm = FlakyLLM({2}, OllamaError("Model returned invalid JSON: {oops"))
        service = make_service(factory, llm)
        results = await service.match_unscored()
        assert [r.job_id for r in results] == [1, 3]
        assert list(service.last_failures) == [2]
        # The failed job is retried next time.
        retry = await make_service(factory).match_unscored()
        assert [r.job_id for r in retry] == [2]

    async def test_ollama_going_down_mid_run_keeps_earlier_scores(
        self, engine: AsyncEngine
    ) -> None:
        factory, _ = await seed(engine, jobs=3)
        llm = FlakyLLM({2, 3}, OllamaUnavailableError("Cannot reach Ollama"))
        service = make_service(factory, llm)
        with pytest.raises(MatchingAbortedError) as excinfo:
            await service.match_unscored()
        assert [r.job_id for r in excinfo.value.scored] == [1]
        async with factory() as session:
            from jobpilot.database.repositories import MatchResultRepository

            remaining = await MatchResultRepository(session).unmatched_job_ids(1)
        assert remaining == [2, 3]  # job 1 was committed before the crash

    async def test_persistent_failures_stop_the_run(self, engine: AsyncEngine) -> None:
        factory, _ = await seed(engine, jobs=8)
        llm = FlakyLLM(set(range(1, 100)), OllamaError("timeout"))
        service = make_service(factory, llm)
        with pytest.raises(MatchingAbortedError, match="in a row"):
            await service.match_unscored()
        assert llm.calls == 5  # MAX_CONSECUTIVE_FAILURES, not all 8

    async def test_long_and_odd_descriptions_are_handled(self, engine: AsyncEngine) -> None:
        factory = create_session_factory(engine)
        async with factory() as session:
            await JobRepository(session).upsert(
                Job(
                    title="Dev",
                    company="Big",
                    application_url="https://a.com/big",
                    source=JobSource.REMOTEOK,
                    description="💥 <b>weird</b>\x00 formatting " * 5000,
                )
            )
            await ResumeRepository(session).add(
                Resume(version="empty", file_path="/r.pdf", is_active=True)
            )
            await session.commit()
        llm = FakeLLM()
        results = await make_service(factory, llm).match_unscored()
        assert len(results) == 1
        assert len(llm.prompts[0]) < 6000  # description truncated
        assert "(none listed)" in llm.prompts[0]  # empty resume handled


class TestRankingRespectsPreferences:
    async def test_exclude_applied_and_blacklisted(self, engine: AsyncEngine) -> None:
        from jobpilot.database.repositories import ApplicationRepository
        from jobpilot.domain import Application

        factory, resume = await seed(engine, jobs=3)
        await make_service(factory).match_unscored()
        async with factory() as session:
            await ApplicationRepository(session).create(Application(job_id=1, resume_id=resume.id))
            await session.commit()
        service = MatchService(
            factory,
            MatchEngine(FakeLLM(), model_name="fake"),
            UserPreferences(blacklist_companies=["acme3"]),
        )
        all_ranked = await service.ranked(top=10)
        assert {r.job.id for r in all_ranked} == {1, 2}  # blacklist always applies
        fresh = await service.ranked(top=10, exclude_applied=True)
        assert [r.job.id for r in fresh] == [2]

    async def test_min_score_filters_on_llm_score(self, engine: AsyncEngine) -> None:
        factory, _ = await seed(engine, jobs=2)
        await make_service(
            factory, FakeLLM({"score": 40, "recommendation": "skip"})
        ).match_unscored()
        service = make_service(factory)
        assert await service.ranked(min_score=41) == []
        assert len(await service.ranked(min_score=40)) == 2
