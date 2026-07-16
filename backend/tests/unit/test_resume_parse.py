from __future__ import annotations

from typing import Any

from jobpilot.domain.models import ResumeProfile
from jobpilot.resume.parse import parse_resume_heuristic, parse_resume_text

SAMPLE_RESUME = """James Doe
Full Stack Developer — 4 years of experience building web applications.

Skills
React, Next.js, TypeScript, Tailwind, Node.js
PostgreSQL, Supabase, REST APIs, Authentication

Projects
• JobBoard — a job aggregation platform built with Next.js and Supabase
• ChatApp — realtime chat using Node.js and WebSockets

Education
BS Computer Science, State University, 2021

Certifications
AWS Certified Cloud Practitioner
"""


class TestHeuristicParse:
    def test_sections_extracted(self) -> None:
        profile = parse_resume_heuristic(SAMPLE_RESUME)
        assert "React" in profile.skills
        assert "PostgreSQL" in profile.skills
        assert any("JobBoard" in p for p in profile.projects)
        assert any("State University" in e for e in profile.education)
        assert any("AWS" in c for c in profile.certifications)

    def test_technologies_scanned_from_whole_text(self) -> None:
        profile = parse_resume_heuristic(SAMPLE_RESUME)
        for tech in ("react", "typescript", "tailwind", "postgresql", "supabase"):
            assert tech in profile.technologies
        assert "rust" not in profile.technologies

    def test_years_experience(self) -> None:
        assert parse_resume_heuristic(SAMPLE_RESUME).years_experience == 4.0
        assert parse_resume_heuristic("no numbers here").years_experience is None

    def test_ats_keywords_lowercase_union(self) -> None:
        profile = parse_resume_heuristic(SAMPLE_RESUME)
        assert "react" in profile.ats_keywords
        assert all(k == k.lower() for k in profile.ats_keywords)


class FakeLLM:
    def __init__(self, payload: dict[str, Any] | None = None, fail: bool = False) -> None:
        self.payload = payload or {}
        self.fail = fail

    async def generate(self, prompt: str, *, system: str | None = None) -> str:
        return ""

    async def generate_json(
        self, prompt: str, *, system: str | None = None, schema: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("llm down")
        return self.payload


class TestLLMParse:
    async def test_no_llm_returns_heuristic(self) -> None:
        profile = await parse_resume_text(SAMPLE_RESUME)
        assert isinstance(profile, ResumeProfile)
        assert "react" in profile.technologies

    async def test_llm_refines_and_merges(self) -> None:
        llm = FakeLLM(
            {
                "skills": ["Frontend Architecture"],
                "technologies": ["react", "graphql"],
                "projects": ["JobBoard — aggregation platform"],
                "education": ["BS CS"],
                "certifications": [],
                "years_experience": 4,
                "ats_keywords": ["frontend"],
            }
        )
        profile = await parse_resume_text(SAMPLE_RESUME, llm)
        assert profile.skills == ["Frontend Architecture"]
        assert "graphql" in profile.technologies  # from LLM
        assert "supabase" in profile.technologies  # merged from heuristic
        assert any("AWS" in c for c in profile.certifications)  # heuristic kept when LLM empty

    async def test_llm_failure_falls_back(self) -> None:
        profile = await parse_resume_text(SAMPLE_RESUME, FakeLLM(fail=True))
        assert "react" in profile.technologies
