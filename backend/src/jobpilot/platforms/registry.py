"""Plugin registry mapping JobSource -> adapter class."""

from __future__ import annotations

from jobpilot.domain.enums import JobSource
from jobpilot.platforms.base import PlatformAdapter

_REGISTRY: dict[JobSource, type[PlatformAdapter]] = {}


def register_platform(cls: type[PlatformAdapter]) -> type[PlatformAdapter]:
    if cls.platform in _REGISTRY:
        raise ValueError(f"An adapter for {cls.platform} is already registered")
    _REGISTRY[cls.platform] = cls
    return cls


def get_adapter_class(platform: JobSource) -> type[PlatformAdapter]:
    try:
        return _REGISTRY[platform]
    except KeyError:
        raise KeyError(f"No adapter registered for platform {platform.value!r}") from None


def all_adapter_classes() -> dict[JobSource, type[PlatformAdapter]]:
    return dict(_REGISTRY)
