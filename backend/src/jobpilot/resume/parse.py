"""Resume text -> ResumeProfile.

Two-stage: heuristic section parsing always runs; if an LLM is available it
refines the result (better project/education extraction, ATS keywords). The
LLM is optional so the pipeline works before Ollama is set up.
"""

from __future__ import annotations

import logging
import re

from jobpilot.domain.models import ResumeProfile
from jobpilot.llm.provider import LLMProvider

logger = logging.getLogger(__name__)

SECTION_ALIASES: dict[str, list[str]] = {
    "skills": ["skills", "technical skills", "core competencies", "technologies", "tech stack"],
    "experience": ["experience", "work experience", "employment", "professional experience"],
    "projects": ["projects", "personal projects", "selected projects", "portfolio"],
    "education": ["education", "academic background"],
    "certifications": ["certifications", "certificates", "licenses"],
}

# Common technology tokens for keyword scanning (lowercase). Deliberately broad;
# matching is word-boundary based so short names don't false-positive.
KNOWN_TECHNOLOGIES = [
    "python",
    "javascript",
    "typescript",
    "java",
    "c#",
    "c++",
    "go",
    "rust",
    "ruby",
    "php",
    "swift",
    "kotlin",
    "sql",
    "html",
    "css",
    "react",
    "next.js",
    "nextjs",
    "vue",
    "angular",
    "svelte",
    "node.js",
    "nodejs",
    "express",
    "django",
    "flask",
    "fastapi",
    "spring",
    "rails",
    "laravel",
    "tailwind",
    "bootstrap",
    "postgresql",
    "postgres",
    "mysql",
    "sqlite",
    "mongodb",
    "redis",
    "supabase",
    "firebase",
    "graphql",
    "rest",
    "grpc",
    "docker",
    "kubernetes",
    "aws",
    "gcp",
    "azure",
    "terraform",
    "git",
    "github",
    "gitlab",
    "ci/cd",
    "jenkins",
    "linux",
    "playwright",
    "selenium",
    "cypress",
    "jest",
    "pytest",
    "pandas",
    "numpy",
    "tensorflow",
    "pytorch",
    "scikit-learn",
    "ollama",
    "langchain",
]

_YEARS_PATTERNS = [
    re.compile(r"(\d{1,2}(?:\.\d)?)\+?\s*years?\s+(?:of\s+)?experience", re.IGNORECASE),
    re.compile(r"experience\D{0,20}(\d{1,2}(?:\.\d)?)\+?\s*years?", re.IGNORECASE),
]


def _split_sections(text: str) -> dict[str, str]:
    """Split resume text into named sections based on heading lines."""
    alias_to_section = {
        alias: section for section, aliases in SECTION_ALIASES.items() for alias in aliases
    }
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        normalized = re.sub(r"[^a-z ]", "", stripped.lower()).strip()
        if normalized in alias_to_section and len(stripped) < 40:
            current = alias_to_section[normalized]
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(stripped)
    return {name: "\n".join(lines).strip() for name, lines in sections.items()}


def _extract_list_items(section_text: str) -> list[str]:
    items: list[str] = []
    for line in section_text.splitlines():
        line = line.strip().lstrip("•-*–·").strip()
        if not line:
            continue
        # Comma/pipe separated skill lines get split; sentence-like lines kept whole.
        if ("," in line or "|" in line) and len(line) < 200:
            items.extend(part.strip() for part in re.split(r"[,|]", line) if part.strip())
        else:
            items.append(line)
    return items


def _scan_technologies(text: str) -> list[str]:
    lowered = text.lower()
    found: list[str] = []
    for tech in KNOWN_TECHNOLOGIES:
        pattern = re.compile(rf"(?<![\w.]){re.escape(tech)}(?![\w])", re.IGNORECASE)
        if pattern.search(lowered):
            found.append(tech)
    return found


def _extract_years_experience(text: str) -> float | None:
    for pattern in _YEARS_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                continue
    return None


def parse_resume_heuristic(text: str) -> ResumeProfile:
    sections = _split_sections(text)
    skills = _extract_list_items(sections.get("skills", ""))
    technologies = _scan_technologies(text)
    ats_keywords = sorted({*(s.lower() for s in skills), *technologies})
    return ResumeProfile(
        skills=skills,
        technologies=technologies,
        projects=_extract_list_items(sections.get("projects", "")),
        education=_extract_list_items(sections.get("education", "")),
        certifications=_extract_list_items(sections.get("certifications", "")),
        years_experience=_extract_years_experience(text),
        ats_keywords=ats_keywords,
    )


_LLM_SYSTEM = (
    "You extract structured data from resumes. Respond with JSON only, matching the "
    "requested schema exactly. Be faithful to the resume text; do not invent anything."
)

_LLM_PROMPT_TEMPLATE = """Extract the following from this resume as JSON:
{{
  "skills": [list of skills],
  "technologies": [specific technologies/tools/frameworks, lowercase],
  "projects": [one-line summaries of projects],
  "education": [degrees/schools],
  "certifications": [certifications],
  "years_experience": total professional years as a number or null,
  "ats_keywords": [keywords an ATS would match for this candidate, lowercase]
}}

Resume:
---
{resume_text}
---"""


async def parse_resume_text(text: str, llm: LLMProvider | None = None) -> ResumeProfile:
    heuristic = parse_resume_heuristic(text)
    if llm is None:
        return heuristic
    try:
        raw = await llm.generate_json(
            _LLM_PROMPT_TEMPLATE.format(resume_text=text[:8000]), system=_LLM_SYSTEM
        )
        llm_profile = ResumeProfile.model_validate(raw)
    except Exception:  # noqa: BLE001 - LLM refinement is best-effort
        logger.warning("LLM resume structuring failed; using heuristic parse", exc_info=True)
        return heuristic

    # Merge: prefer LLM output, keep heuristic values it missed.
    return ResumeProfile(
        skills=llm_profile.skills or heuristic.skills,
        technologies=sorted({*llm_profile.technologies, *heuristic.technologies}),
        projects=llm_profile.projects or heuristic.projects,
        education=llm_profile.education or heuristic.education,
        certifications=llm_profile.certifications or heuristic.certifications,
        years_experience=llm_profile.years_experience or heuristic.years_experience,
        ats_keywords=sorted({*llm_profile.ats_keywords, *heuristic.ats_keywords}),
    )
