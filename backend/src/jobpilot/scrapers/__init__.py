"""HTTP plumbing and API clients shared by platform adapters (see jobpilot.platforms)."""

from jobpilot.scrapers.http import RateLimiter, create_http_client, get_with_retry
from jobpilot.scrapers.remoteok import RemoteOKScraper

__all__ = ["RateLimiter", "RemoteOKScraper", "create_http_client", "get_with_retry"]
