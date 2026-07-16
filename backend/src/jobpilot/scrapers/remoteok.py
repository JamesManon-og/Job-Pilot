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
from jobpilot.scrapers.base import BaseScraper
from jobpilot.scrapers.http import RateLimiter, create_http_client, get_with_retry
from jobpilot.scrapers.registry import register_scraper

logger = logging.getLogger(__name__)

API_URL = "https://remoteok.com/api"


def _html_to_text(html: str) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(separator="\n", strip=True)


def _parse_entry(entry: dict[str, Any]) -> Job | None:
    position = (entry.get("position") or "").strip()
    company = (entry.get("company") or "").strip()
    url = (entry.get("url") or entry.get("apply_url") or "").strip()
    if not position or not company or not url:
        return None

    date_posted: datetime | None = None
    epoch = entry.get("epoch")
    if epoch:
        try:
            date_posted = datetime.fromtimestamp(int(epoch), tz=UTC)
        except (ValueError, OSError):
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

    tags = [str(tag).strip().lower() for tag in entry.get("tags") or [] if str(tag).strip()]

    return Job(
        title=position,
        company=company,
        location=(entry.get("location") or "").strip() or None,
        salary_raw=salary_raw,
        salary_min=salary_min,
        salary_max=salary_max,
        remote=RemoteType.REMOTE,
        description=_html_to_text(entry.get("description") or ""),
        technologies=tags,
        application_url=url,
        source=JobSource.REMOTEOK,
        date_posted=date_posted,
    )


@register_scraper
class RemoteOKScraper(BaseScraper):
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
            job = _parse_entry(entry)
            if job is None:
                logger.debug("Skipping incomplete RemoteOK entry: %s", entry.get("id"))
                continue
            yield job
