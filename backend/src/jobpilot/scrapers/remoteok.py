"""RemoteOK scraper — public JSON API at https://remoteok.com/api.

The API returns a JSON array whose first element is a legal notice object;
actual job postings follow. All RemoteOK listings are remote by definition.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, ClassVar

from bs4 import BeautifulSoup

from jobpilot.domain.enums import JobSource, RemoteType
from jobpilot.domain.models import Job
from jobpilot.scrapers.http import RateLimiter, create_http_client, get_with_retry

logger = logging.getLogger(__name__)

API_URL = "https://remoteok.com/api"


def _html_to_text(html: str) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(separator="\n", strip=True)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _parse_entry(entry: dict[str, Any]) -> Job | None:
    position = _text(entry.get("position"))
    company = _text(entry.get("company"))
    url = _text(entry.get("url")) or _text(entry.get("apply_url"))
    if not position or not company or not url.startswith(("http://", "https://")):
        return None

    date_posted: datetime | None = None
    epoch = entry.get("epoch")
    if epoch:
        try:
            date_posted = datetime.fromtimestamp(int(epoch), tz=UTC)
        except (ValueError, OSError, OverflowError):
            date_posted = None

    def _int_or_none(value: Any) -> int | None:
        try:
            return int(value) if value else None
        except (TypeError, ValueError):
            return None

    salary_min = _int_or_none(entry.get("salary_min"))
    salary_max = _int_or_none(entry.get("salary_max"))
    salary_raw = None
    if salary_min or salary_max:
        salary_raw = f"${salary_min or '?'} - ${salary_max or '?'}"

    raw_tags = entry.get("tags")
    if isinstance(raw_tags, str):  # a bare string would otherwise iterate as characters
        raw_tags = raw_tags.split(",")
    if not isinstance(raw_tags, list):
        raw_tags = []
    tags = [str(tag).strip().lower() for tag in raw_tags if str(tag).strip()]

    return Job(
        title=position,
        company=company,
        location=_text(entry.get("location")) or None,
        salary_raw=salary_raw,
        salary_min=salary_min,
        salary_max=salary_max,
        remote=RemoteType.REMOTE,
        description=_html_to_text(_text(entry.get("description"))),
        technologies=tags,
        application_url=url,
        source=JobSource.REMOTEOK,
        external_id=str(entry.get("id") or "").strip() or None,
        date_posted=date_posted,
    )


class RemoteOKScraper:
    """API client used by platforms.remoteok.RemoteOKAdapter."""

    source: ClassVar[JobSource] = JobSource.REMOTEOK

    def __init__(self, api_url: str = API_URL) -> None:
        self._api_url = api_url
        self._limiter = RateLimiter(min_interval_seconds=2.0)

    async def scrape(self) -> AsyncIterator[Job]:
        async with create_http_client() as client:
            response = await get_with_retry(client, self._api_url, limiter=self._limiter)
            payload = response.json()

        if not isinstance(payload, list):
            raise ValueError(f"Unexpected RemoteOK payload type: {type(payload).__name__}")

        for entry in payload:
            if not isinstance(entry, dict):
                continue
            if "legal" in entry and "position" not in entry:
                continue  # first element is the API's legal notice
            try:
                job = _parse_entry(entry)
            except Exception:  # noqa: BLE001 - one malformed listing must not kill the scrape
                logger.warning(
                    "Skipping malformed RemoteOK entry %s", entry.get("id"), exc_info=True
                )
                continue
            if job is None:
                logger.debug("Skipping incomplete RemoteOK entry: %s", entry.get("id"))
                continue
            yield job
