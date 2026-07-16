"""LLM-based job/resume matching."""

from __future__ import annotations

import logging

from jobpilot.domain.enums import MatchRecommendation
from jobpilot.domain.models import Job, MatchResult, ResumeProfile
from jobpilot.llm.provider import LLMProvider

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are a precise job-matching assistant. Compare a candidate's resume profile "
    "against a job posting and respond with JSON only. Score honestly: 90+ means the "
    "candidate matches nearly every requirement; below 50 means fundamental gaps."
)

_PROMPT_TEMPLATE = """Candidate profile:
- Skills: {skills}
- Technologies: {technologies}
- Years of experience: {years}
- Projects: {projects}
- Education: {education}
- Certifications: {certifications}

Job posting:
- Title: {title}
- Company: {company}
- Technologies mentioned: {job_technologies}
- Description:
{description}

Respond with JSON:
{{
  "score": <0-100 integer, how well the candidate fits this job>,
  "matched_skills": [candidate skills/technologies this job asks for],
  "missing_skills": [important requirements the candidate lacks],
  "recommendation": "strong_apply" | "apply" | "maybe" | "skip",
  "reasoning": "2-3 sentence explanation"
}}"""

_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "matched_skills": {"type": "array", "items": {"type": "string"}},
        "missing_skills": {"type": "array", "items": {"type": "string"}},
        "recommendation": {
            "type": "string",
            "enum": ["strong_apply", "apply", "maybe", "skip"],
        },
        "reasoning": {"type": "string"},
    },
    "required": ["score", "matched_skills", "missing_skills", "recommendation", "reasoning"],
}


def _clamp_score(value: object) -> int:
    try:
        score = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, int(score)))


def _coerce_recommendation(value: object, score: int) -> MatchRecommendation:
    try:
        return MatchRecommendation(str(value))
    except ValueError:
        # Model returned something off-menu; derive from the score instead.
        if score >= 85:
            return MatchRecommendation.STRONG_APPLY
        if score >= 70:
            return MatchRecommendation.APPLY
        if score >= 50:
            return MatchRecommendation.MAYBE
        return MatchRecommendation.SKIP


class MatchEngine:
    def __init__(self, llm: LLMProvider, *, model_name: str = "") -> None:
        self._llm = llm
        self._model_name = model_name

    async def match(self, job: Job, profile: ResumeProfile, *, resume_id: int) -> MatchResult:
        assert job.id is not None
        prompt = _PROMPT_TEMPLATE.format(
            skills=", ".join(profile.skills) or "(none listed)",
            technologies=", ".join(profile.technologies) or "(none listed)",
            years=profile.years_experience or "unknown",
            projects="; ".join(profile.projects[:8]) or "(none listed)",
            education="; ".join(profile.education) or "(none listed)",
            certifications="; ".join(profile.certifications) or "(none)",
            title=job.title,
            company=job.company,
            job_technologies=", ".join(job.technologies) or "(none tagged)",
            description=job.description[:4000],
        )
        raw = await self._llm.generate_json(prompt, system=_SYSTEM, schema=_SCHEMA)

        score = _clamp_score(raw.get("score"))
        return MatchResult(
            job_id=job.id,
            resume_id=resume_id,
            score=score,
            matched_skills=[str(s) for s in raw.get("matched_skills") or []],
            missing_skills=[str(s) for s in raw.get("missing_skills") or []],
            recommendation=_coerce_recommendation(raw.get("recommendation"), score),
            reasoning=str(raw.get("reasoning") or ""),
            llm_model=self._model_name,
        )
