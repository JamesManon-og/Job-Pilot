"""Ollama-backed LLMProvider implementation (local models via the Ollama server)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class OllamaError(Exception):
    """Raised when the Ollama server is unreachable or returns garbage."""


class OllamaUnavailableError(OllamaError):
    """The server can't be reached at all; retrying other jobs is pointless."""


# Reasoning models (qwen3, deepseek-r1) emit <think>…</think> preambles; older
# Ollama versions ignore `think: false`, so strip them defensively.
_THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


def strip_reasoning(text: str) -> str:
    text = _THINK_BLOCK.sub("", text)
    if "</think>" in text.lower():  # unterminated opening tag: keep what follows
        text = re.split(r"</think>", text, flags=re.IGNORECASE)[-1]
    return text.strip()


class OllamaClient:
    """Implements the LLMProvider protocol against a local Ollama server."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "qwen3:8b",
        *,
        timeout_seconds: float = 180.0,
        attempts: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = httpx.Timeout(timeout_seconds, connect=5.0)
        self._attempts = attempts
        self._transport = transport

    def _http(self, timeout: httpx.Timeout | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout or self._timeout, transport=self._transport)

    @property
    def model(self) -> str:
        return self._model

    async def _chat(self, prompt: str, *, system: str | None, json_format: bool) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            # Deterministic-ish output for scoring/extraction tasks.
            "options": {"temperature": 0.2},
            # qwen3 supports disabling its thinking mode for faster structured output.
            "think": False,
        }
        if json_format:
            payload["format"] = "json"

        last_error: Exception | None = None
        for attempt in range(self._attempts):
            try:
                async with self._http() as client:
                    response = await client.post(f"{self._base_url}/api/chat", json=payload)
                    response.raise_for_status()
                    data = response.json()
                message = data.get("message") if isinstance(data, dict) else None
                content = message.get("content", "") if isinstance(message, dict) else ""
                if not isinstance(content, str):
                    raise OllamaError(f"Malformed response from model {self._model}")
                content = strip_reasoning(content)
                if not content:
                    raise OllamaError(f"Empty response from model {self._model}")
                return content
            except httpx.ConnectError as exc:
                raise OllamaUnavailableError(
                    f"Cannot reach Ollama at {self._base_url}. Is it running? "
                    "(brew services start ollama)"
                ) from exc
            except ValueError as exc:  # response body wasn't JSON
                last_error = OllamaError(f"Non-JSON response from Ollama: {exc}")
                if attempt < self._attempts - 1:
                    await asyncio.sleep(2.0)
                continue
            except (httpx.HTTPError, OllamaError) as exc:
                last_error = exc
                if attempt < self._attempts - 1:
                    logger.warning("Ollama call failed (%s); retrying", exc)
                    await asyncio.sleep(2.0)
        raise OllamaError(f"Ollama call failed after {self._attempts} attempts: {last_error}")

    async def generate(self, prompt: str, *, system: str | None = None) -> str:
        return await self._chat(prompt, system=system, json_format=False)

    async def generate_json(
        self, prompt: str, *, system: str | None = None, schema: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if schema is not None:
            prompt = f"{prompt}\n\nRespond with JSON matching this schema:\n{json.dumps(schema)}"
        content = await self._chat(prompt, system=system, json_format=True)
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise OllamaError(f"Model returned invalid JSON: {content[:200]}") from exc
        if not isinstance(parsed, dict):
            raise OllamaError(f"Expected a JSON object, got {type(parsed).__name__}")
        return parsed

    async def is_available(self) -> bool:
        try:
            async with self._http(httpx.Timeout(5.0)) as client:
                response = await client.get(f"{self._base_url}/api/version")
                return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def has_model(self) -> bool:
        try:
            async with self._http(httpx.Timeout(5.0)) as client:
                response = await client.get(f"{self._base_url}/api/tags")
                response.raise_for_status()
                models = [m.get("name", "") for m in response.json().get("models", [])]
            base = self._model.split(":")[0]
            return any(name == self._model or name.startswith(f"{base}:") for name in models)
        except httpx.HTTPError:
            return False
