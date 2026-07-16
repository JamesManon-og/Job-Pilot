from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from jobpilot.llm.ollama import OllamaClient, OllamaError


def chat_handler(content: str) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        return httpx.Response(200, json={"message": {"role": "assistant", "content": content}})

    return handler


def make_client(content: str, *, attempts: int = 1) -> OllamaClient:
    return OllamaClient(
        base_url="http://testserver",
        attempts=attempts,
        transport=httpx.MockTransport(chat_handler(content)),
    )


class TestOllamaClient:
    async def test_generate_returns_text(self) -> None:
        client = make_client("hello there")
        assert await client.generate("hi") == "hello there"

    async def test_generate_json_parses_object(self) -> None:
        client = make_client(json.dumps({"score": 88}))
        assert await client.generate_json("score this") == {"score": 88}

    async def test_generate_json_rejects_invalid(self) -> None:
        client = make_client("not json at all")
        with pytest.raises(OllamaError, match="invalid JSON"):
            await client.generate_json("score this")

    async def test_generate_json_rejects_non_object(self) -> None:
        client = make_client(json.dumps([1, 2]))
        with pytest.raises(OllamaError, match="JSON object"):
            await client.generate_json("score this")

    async def test_json_format_flag_sent(self) -> None:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(json.loads(request.content))
            return httpx.Response(200, json={"message": {"content": "{}"}})

        client = OllamaClient(
            base_url="http://testserver", attempts=1, transport=httpx.MockTransport(handler)
        )
        await client.generate_json("x", system="sys")
        assert seen["format"] == "json"
        messages = seen["messages"]
        assert isinstance(messages, list) and messages[0]["role"] == "system"

    async def test_empty_response_retries_then_fails(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json={"message": {"content": ""}})

        client = OllamaClient(
            base_url="http://testserver", attempts=2, transport=httpx.MockTransport(handler)
        )
        with pytest.raises(OllamaError, match="after 2 attempts"):
            await client.generate("hi")
        assert calls["n"] == 2

    async def test_connect_error_message_mentions_server(self) -> None:
        client = OllamaClient(base_url="http://127.0.0.1:59999", attempts=1)
        with pytest.raises(OllamaError, match="Cannot reach Ollama"):
            await client.generate("hi")

    async def test_has_model_matches_tag(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/tags"
            return httpx.Response(200, json={"models": [{"name": "qwen3:8b"}]})

        client = OllamaClient(base_url="http://testserver", transport=httpx.MockTransport(handler))
        assert await client.has_model() is True
