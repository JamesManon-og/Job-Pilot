"""Shared HTTP plumbing for scrapers: polite client + rate limiter."""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

logger = logging.getLogger(__name__)

USER_AGENT = "JobPilot/0.1 (personal job-search assistant; contact: local user)"

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def create_http_client(*, headers: dict[str, str] | None = None) -> httpx.AsyncClient:
    merged = {"User-Agent": USER_AGENT}
    if headers:
        merged.update(headers)
    return httpx.AsyncClient(
        headers=merged,
        timeout=DEFAULT_TIMEOUT,
        follow_redirects=True,
        transport=httpx.AsyncHTTPTransport(retries=2),
    )


class RateLimiter:
    """Enforces a minimum interval between requests (per limiter instance)."""

    def __init__(self, min_interval_seconds: float = 1.0) -> None:
        self._min_interval = min_interval_seconds
        self._last_request = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request
            if elapsed < self._min_interval:
                await asyncio.sleep(self._min_interval - elapsed)
            self._last_request = time.monotonic()


async def get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    limiter: RateLimiter | None = None,
    attempts: int = 3,
    backoff_seconds: float = 2.0,
) -> httpx.Response:
    """GET with rate limiting and exponential backoff on 429/5xx/transport errors."""
    last_error: Exception | None = None
    for attempt in range(attempts):
        if limiter is not None:
            await limiter.wait()
        try:
            response = await client.get(url)
            if response.status_code == 429 or response.status_code >= 500:
                raise httpx.HTTPStatusError(
                    f"retryable status {response.status_code}",
                    request=response.request,
                    response=response,
                )
            response.raise_for_status()
            return response
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            if isinstance(exc, httpx.HTTPStatusError):
                status = exc.response.status_code
                if status not in (429,) and status < 500:
                    raise  # 4xx other than 429: retrying won't help
            last_error = exc
            if attempt < attempts - 1:
                delay = backoff_seconds * (2**attempt)
                logger.warning("GET %s failed (%s); retrying in %.1fs", url, exc, delay)
                await asyncio.sleep(delay)
    assert last_error is not None
    raise last_error
