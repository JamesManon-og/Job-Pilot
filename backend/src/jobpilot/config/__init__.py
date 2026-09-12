from jobpilot.config.preferences import PreferencesError, UserPreferences, load_preferences
from jobpilot.config.settings import Settings, get_settings


def load_user_preferences(settings: Settings | None = None) -> UserPreferences:
    """The user's config.yaml, with relative paths resolved against the project root."""
    settings = settings or get_settings()
    return load_preferences(settings.preferences_path, project_root=settings.project_root)


__all__ = [
    "PreferencesError",
    "Settings",
    "UserPreferences",
    "get_settings",
    "load_preferences",
    "load_user_preferences",
]
