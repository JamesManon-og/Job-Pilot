"""Retry/backoff for scrapers (previously untested)."""

from __future__ import annotations

import httpx
import pytest

from jobpilot.scrapers import http as scraper_http
from jobpilot.scrapers.http import MAX_RETRY_AFTER_SECONDS, get_with_retry


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(scraper_http.asyncio, "sleep", fake_sleep)
    return recorded


def client_for(*responses: httpx.Response | Exception) -> tuple[httpx.AsyncClient, list[int]]:
    calls: list[int] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), calls


async def test_retries_server_errors_then_succeeds(sleeps: list[float]) -> None:
    client, calls = client_for(httpx.Response(502), httpx.Response(200, text="ok"))
    response = await get_with_retry(client, "https://x.test/api", backoff_seconds=1)
    assert response.text == "ok" and len(calls) == 2
    assert sleeps == [1]


async def test_client_errors_are_not_retried(sleeps: list[float]) -> None:
    client, calls = client_for(httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        await get_with_retry(client, "https://x.test/api")
    assert len(calls) == 1 and sleeps == []


async def test_rate_limit_honors_retry_after(sleeps: list[float]) -> None:
    client, _ = client_for(httpx.Response(429, headers={"Retry-After": "30"}), httpx.Response(200))
    await get_with_retry(client, "https://x.test/api", backoff_seconds=1)
    assert sleeps == [30]


async def test_hostile_retry_after_is_capped(sleeps: list[float]) -> None:
    client, _ = client_for(
        httpx.Response(429, headers={"Retry-After": "999999"}), httpx.Response(200)
    )
    await get_with_retry(client, "https://x.test/api")
    assert sleeps == [MAX_RETRY_AFTER_SECONDS]


async def test_network_errors_retry_then_give_up(sleeps: list[float]) -> None:
    request = httpx.Request("GET", "https://x.test/api")
    client, calls = client_for(*(httpx.ConnectTimeout("t", request=request) for _ in range(3)))
    with pytest.raises(httpx.ConnectTimeout):
        await get_with_retry(client, "https://x.test/api", attempts=3, backoff_seconds=1)
    assert len(calls) == 3
    assert sleeps == [1, 2]  # exponential, no sleep after the last attempt
