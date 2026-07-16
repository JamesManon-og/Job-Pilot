"""Scraper plugin contract (framework lands in Milestone 3).

Every job board integration subclasses BaseScraper, declares its JobSource,
and registers itself with @register_scraper. Adding a new board is one module.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import ClassVar

from jobpilot.domain.enums import JobSource
from jobpilot.domain.models import Job


class BaseScraper(ABC):
    source: ClassVar[JobSource]

    @abstractmethod
    def scrape(self) -> AsyncIterator[Job]:
        """Yield normalized Job entities from the source."""
        raise NotImplementedError
