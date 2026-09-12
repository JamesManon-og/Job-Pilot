"""Job-board adapters. Importing this package registers every adapter."""

from jobpilot.config.preferences import UserPreferences
from jobpilot.domain.enums import JobSource
from jobpilot.platforms import (  # noqa: F401 - imported so adapters self-register
    jobstreet,
    linkedin,
    onlinejobs,
    remoteok,
)
from jobpilot.platforms.base import (
    ChallengeError,
    PlatformAdapter,
    PlatformError,
    SearchQuery,
    SessionExpiredError,
    matches_keywords,
)
from jobpilot.platforms.browser import BrowserProfiles, ProfileInUseError
from jobpilot.platforms.registry import all_adapter_classes, get_adapter_class, register_platform
from jobpilot.platforms.runner import SearchRunner, SearchStats


def enabled_platforms(preferences: UserPreferences) -> list[JobSource]:
    """Registered platforms switched on in config.yaml (`platforms.<name>.enabled`)."""
    return [p for p in all_adapter_classes() if preferences.platform(p.value).enabled]


__all__ = [
    "BrowserProfiles",
    "ChallengeError",
    "PlatformAdapter",
    "PlatformError",
    "ProfileInUseError",
    "SearchQuery",
    "SearchRunner",
    "SearchStats",
    "SessionExpiredError",
    "all_adapter_classes",
    "enabled_platforms",
    "get_adapter_class",
    "matches_keywords",
    "register_platform",
]
