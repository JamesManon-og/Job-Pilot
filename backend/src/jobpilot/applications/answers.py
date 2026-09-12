"""LLM-proposed answers for application questions JobPilot couldn't fill.

Proposals go back to the review queue — they're never typed into a form
until the human has read and approved them. Sensitive questions (identity
documents, demographics, salary history…) never reach this code: the
autofill engine sets them aside before anything is proposed.
"""

from __future__ import annotations

import logging

from jobpilot.config.preferences import ApplicantProfile
from jobpilot.domain.models import Job, ResumeProfile
from jobpilot.llm.provider import LLMProvider

logger = logging.getLogger(__name__)

MAX_QUESTIONS = 12

_SYSTEM = (
    "You draft answers to job-application questions for a candidate. Use ONLY facts in "
    "the candidate profile. If the profile doesn't support an honest answer, return an "
    "empty string for that question — never guess numbers, dates, legal status, or "
    "experience. Keep each answer under 80 words. Respond with JSON only."
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "answers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"question": {"type": "string"}, "answer": {"type": "string"}},
                "required": ["question", "answer"],
            },
        }
    },
    "required": ["answers"],
}


class AnswerProposer:
    def __init__(
        self, llm: LLMProvider, applicant: ApplicantProfile, profile: ResumeProfile
    ) -> None:
        self._llm = llm
        self._applicant = applicant
        self._profile = profile

    async def propose(self, job: Job, questions: list[str]) -> dict[str, str]:
        """Question -> proposed answer, only for questions it could answer honestly."""
        questions = [q for q in dict.fromkeys(q.strip() for q in questions) if q][:MAX_QUESTIONS]
        if not questions:
            return {}
        listing = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
        prompt = (
            "Candidate profile:\n"
            f"- Name: {self._applicant.name or 'not provided'}\n"
            f"- Location: {self._applicant.location or self._applicant.address or 'not provided'}\n"
            f"- Technologies: {', '.join(self._profile.technologies) or 'none listed'}\n"
            f"- Skills: {', '.join(self._profile.skills[:25]) or 'none listed'}\n"
            f"- Years of experience: {self._profile.years_experience or 'not stated'}\n"
            f"- Projects: {'; '.join(self._profile.projects[:5]) or 'none listed'}\n"
            f"- Education: {'; '.join(self._profile.education[:3]) or 'none listed'}\n\n"
            f"Job: {job.title} at {job.company}\n{job.description[:1500]}\n\n"
            f"Questions from the application form:\n{listing}\n\n"
            'Return {"answers": [{"question": <exact question>, "answer": <answer or "">}]}.'
        )
        raw = await self._llm.generate_json(prompt, system=_SYSTEM, schema=_SCHEMA)
        items = raw.get("answers")
        if not isinstance(items, list):
            return {}
        proposed: dict[str, str] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            question, answer = str(item.get("question", "")).strip(), str(item.get("answer", ""))
            if question in questions and answer.strip():
                proposed[question] = answer.strip()
        logger.info("Proposed %d/%d answers for job %s", len(proposed), len(questions), job.id)
        return proposed
