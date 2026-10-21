"""Authenticated, owner-scoped controls and history for scheduled discovery."""
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import Principal, require_user
from .config import Settings, get_settings
from . import scheduled_store

router = APIRouter(prefix="/api/v1/discovery/schedule", tags=["discovery"])


class ScheduleUpdate(BaseModel):
    enabled: bool


class ScheduledRunSummary(BaseModel):
    id: str
    status: str
    started_at: str | None = None
    finished_at: str | None = None
    source_results: list[dict[str, Any]] = Field(default_factory=list)
    new_discoveries: int = 0
    updated_discoveries: int = 0
    error_summary: str | None = None
    attempt_count: int = 1


class ScheduleResponse(BaseModel):
    configured: bool
    worker_deployed: bool
    enabled: bool
    schedule_hour_utc: int
    timezone: str = "UTC"
    next_scheduled_at: str | None = None
    recent_runs: list[ScheduledRunSummary] = Field(default_factory=list)
    detail: str | None = None


def _next_run(hour: int, now: datetime | None = None) -> str:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    next_time = current.replace(hour=hour, minute=0, second=0, microsecond=0)
    if next_time <= current:
        next_time += timedelta(days=1)
    return next_time.isoformat().replace("+00:00", "Z")


def _configured(settings: Settings) -> bool:
    return settings.agent_state_storage == "supabase" and settings.supabase_storage_configured


@router.get("", response_model=ScheduleResponse)
def get_schedule(principal: Principal = Depends(require_user), settings: Settings = Depends(get_settings)):
    configured = _configured(settings)
    base = {
        "configured": configured,
        "worker_deployed": settings.scheduled_discovery_worker_deployed,
        "enabled": False,
        "schedule_hour_utc": settings.scheduled_discovery_hour_utc,
        "next_scheduled_at": None,
        "recent_runs": [],
    }
    if not configured:
        return ScheduleResponse(
            **base,
            detail="Cloud scheduling needs AGENT_STATE_STORAGE=supabase, SUPABASE_URL, and SUPABASE_PUBLISHABLE_KEY.",
        )
    if not principal.access_token:
        raise HTTPException(status_code=401, detail="A verified Supabase session is required for schedule settings.")
    try:
        enabled, rows = scheduled_store.get_user_schedule(principal.user_id, principal.access_token, settings)
        runs = [ScheduledRunSummary(**row) for row in rows]
    except scheduled_store.ScheduledStoreError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return ScheduleResponse(
        **{**base, "enabled": enabled, "next_scheduled_at": _next_run(settings.scheduled_discovery_hour_utc) if enabled else None, "recent_runs": runs},
        detail=None if settings.scheduled_discovery_worker_deployed else "The setting is saved, but the Render Cron worker is not marked as deployed.",
    )


@router.post("", response_model=ScheduleResponse)
def update_schedule(
    request: ScheduleUpdate,
    principal: Principal = Depends(require_user),
    settings: Settings = Depends(get_settings),
):
    if not _configured(settings):
        raise HTTPException(
            status_code=503,
            detail="Cloud scheduling needs AGENT_STATE_STORAGE=supabase, SUPABASE_URL, and SUPABASE_PUBLISHABLE_KEY.",
        )
    if not principal.access_token:
        raise HTTPException(status_code=401, detail="A verified Supabase session is required for schedule settings.")
    try:
        scheduled_store.set_user_schedule(principal.user_id, request.enabled, principal.access_token, settings)
    except scheduled_store.ScheduledStoreError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return get_schedule(principal, settings)
