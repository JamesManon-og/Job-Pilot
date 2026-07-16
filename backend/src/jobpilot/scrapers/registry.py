"""Plugin registry mapping JobSource -> scraper class."""

from __future__ import annotations

from jobpilot.domain.enums import JobSource
from jobpilot.scrapers.base import BaseScraper

_REGISTRY: dict[JobSource, type[BaseScraper]] = {}


def register_scraper(cls: type[BaseScraper]) -> type[BaseScraper]:
    if cls.source in _REGISTRY:
        raise ValueError(f"A scraper for {cls.source} is already registered")
    _REGISTRY[cls.source] = cls
    return cls


def get_scraper(source: JobSource) -> type[BaseScraper]:
    try:
        return _REGISTRY[source]
    except KeyError:
        raise KeyError(f"No scraper registered for source {source!r}") from None


def all_scrapers() -> dict[JobSource, type[BaseScraper]]:
    return dict(_REGISTRY)
