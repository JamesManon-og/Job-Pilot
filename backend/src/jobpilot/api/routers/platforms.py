"""Platform list: what's enabled, what's logged in, what needs attention."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from jobpilot.api.deps import SessionDep, StateDep
from jobpilot.database.repositories import PlatformSessionRepository
from jobpilot.domain.enums import SessionStatus
from jobpilot.platforms import BrowserProfiles, all_adapter_classes

router = APIRouter(prefix="/api/platforms", tags=["platforms"])


class PlatformInfo(BaseModel):
    platform: str
    display_name: str
    enabled: bool
    needs_account: bool  # False for public APIs like RemoteOK
    has_saved_login: bool
    session_status: str
    session_detail: str = ""
    last_checked_at: str | None = None
    login_command: str | None = None


@router.get("", response_model=list[PlatformInfo])
async def list_platforms(session: SessionDep, state: StateDep) -> list[PlatformInfo]:
    preferences = state.preferences()
    settings = state.settings
    profiles = BrowserProfiles(settings.profiles_dir, settings.locks_dir)
    saved = {s.platform: s for s in await PlatformSessionRepository(session).list()}
    infos = []
    for platform, adapter in all_adapter_classes().items():
        record = saved.get(platform)
        needs_account = adapter.login_url is not None
        status = record.status if record else SessionStatus.UNKNOWN
        infos.append(
            PlatformInfo(
                platform=platform.value,
                display_name=adapter.display_name,
                enabled=preferences.platform(platform.value).enabled,
                needs_account=needs_account,
                has_saved_login=profiles.has_profile(platform),
                session_status=(
                    SessionStatus.NOT_REQUIRED.value if not needs_account else status.value
                ),
                session_detail=record.detail if record else "",
                last_checked_at=(
                    record.last_checked_at.isoformat()
                    if record and record.last_checked_at
                    else None
                ),
                login_command=f"jobpilot login {platform.value}" if needs_account else None,
            )
        )
    return infos
