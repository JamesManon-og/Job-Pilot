"""LLM abstraction (Ollama implementation lands in Milestone 6).

Services depend on this protocol, never on a concrete client, so the
backing model can be swapped and tests can inject a fake.
"""

from __future__ import annotations

from typing import Any, Protocol


class LLMProvider(Protocol):
    async def generate(self, prompt: str, *, system: str | None = None) -> str:
        """Return the model's text completion for the prompt."""
        ...

    async def generate_json(
        self, prompt: str, *, system: str | None = None, schema: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Return a structured (JSON) completion, optionally constrained by a schema."""
        ...
