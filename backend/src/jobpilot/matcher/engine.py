"""LLM-based job/resume matching."""

from __future__ import annotations

import logging
import re

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


class MalformedMatchError(ValueError):
    """The model's reply had no usable score; don't record a fake 0."""


_SCORE_PATTERN = re.compile(r"-?\d+(?:\.\d+)?")


def _clamp_score(value: object) -> int:
    """Coerce the model's score to 0-100: accepts 85, 85.4, "85", "85%", "85/100"."""
    if isinstance(value, bool):
        raise MalformedMatchError(f"score is a boolean: {value!r}")
    if isinstance(value, int | float):
        score = float(value)
    elif isinstance(value, str) and (found := _SCORE_PATTERN.search(value)):
        score = float(found.group())
    else:
        raise MalformedMatchError(f"no numeric score in model output: {value!r}")
    return max(0, min(100, round(score)))


def _as_str_list(value: object) -> list[str]:
    """Models sometimes return "react, node" instead of ["react", "node"]."""
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[,;\n]", value) if part.strip()]
    return []


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
            matched_skills=_as_str_list(raw.get("matched_skills")),
            missing_skills=_as_str_list(raw.get("missing_skills")),
            recommendation=_coerce_recommendation(raw.get("recommendation"), score),
            reasoning=str(raw.get("reasoning") or ""),
            llm_model=self._model_name,
        )
