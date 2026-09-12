"""LLM-proposed answers: grounded prompt, and nothing the model invents gets through."""

from __future__ import annotations

from typing import Any

from jobpilot.applications.answers import AnswerProposer
from jobpilot.config.preferences import ApplicantProfile
from jobpilot.domain import Job, JobSource, ResumeProfile


class ScriptedLLM:
    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply
        self.prompt = ""

    async def generate(self, prompt: str, *, system: str | None = None) -> str:
        raise AssertionError("not used")

    async def generate_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        self.prompt = prompt
        return self.reply


JOB = Job(title="Dev", company="Acme", application_url="https://a.test/1", source=JobSource.OTHER)


async def test_only_honest_answers_to_asked_questions_are_kept() -> None:
    llm = ScriptedLLM(
        {
            "answers": [
                {"question": "Describe a project", "answer": "Built MoneyApp."},
                {"question": "Years with Rust?", "answer": ""},  # can't answer honestly
                {"question": "Invented question", "answer": "Sneaky"},
                "garbage",
            ]
        }
    )
    proposer = AnswerProposer(
        llm,  # type: ignore[arg-type]
        ApplicantProfile(name="J"),
        ResumeProfile(technologies=["react"], projects=["MoneyApp"]),
    )
    got = await proposer.propose(
        JOB, ["Describe a project", "Years with Rust?", "Describe a project"]
    )
    assert got == {"Describe a project": "Built MoneyApp."}
    assert "MoneyApp" in llm.prompt and "react" in llm.prompt
    assert llm.prompt.count("Describe a project") == 1  # de-duplicated


async def test_malformed_reply_proposes_nothing() -> None:
    proposer = AnswerProposer(
        ScriptedLLM({"answers": "nope"}),  # type: ignore[arg-type]
        ApplicantProfile(),
        ResumeProfile(),
    )
    assert await proposer.propose(JOB, ["Q?"]) == {}
    assert await proposer.propose(JOB, []) == {}
