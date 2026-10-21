"""Supabase persistence for user schedule controls and the isolated cron worker."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import httpx

from .config import Settings


class ScheduledStoreError(RuntimeError):
    status_code = 503


class ScheduledStoreAuthError(ScheduledStoreError):
    status_code = 401


class ScheduledStorePermissionError(ScheduledStoreError):
    status_code = 403


def _request(
    settings: Settings,
    api_key: str,
    bearer: str,
    method: str,
    resource: str,
    *,
    params: dict[str, str] | None = None,
    json: Any = None,
    prefer: str | None = None,
) -> list[dict[str, Any]]:
    headers = {"apikey": api_key, "Authorization": f"Bearer {bearer}", "Accept": "application/json"}
    if json is not None:
        headers["Content-Type"] = "application/json"
    if prefer:
        headers["Prefer"] = prefer
    try:
        response = httpx.request(
            method,
            f"{settings.supabase_url.rstrip('/')}/rest/v1/{resource}",
            params=params,
            json=json,
            headers=headers,
            timeout=settings.supabase_request_timeout_seconds,
        )
    except httpx.TimeoutException as exc:
        raise ScheduledStoreError("Supabase scheduled-discovery request timed out.") from exc
    except httpx.RequestError as exc:
        raise ScheduledStoreError("Supabase scheduled-discovery storage is unavailable.") from exc
    if response.status_code == 401:
        raise ScheduledStoreAuthError("Supabase rejected the authenticated session for scheduled discovery.")
    if response.status_code == 403:
        raise ScheduledStorePermissionError("Supabase denied access to this user's schedule or run history.")
    if response.status_code == 404:
        raise ScheduledStoreError("Scheduled discovery tables are missing; apply migration 202610200003_scheduled_discovery.sql.")
    if not response.is_success:
        # Do not forward provider response bodies; they may contain sensitive data.
        raise ScheduledStoreError(f"Supabase scheduled-discovery request failed with HTTP {response.status_code}.")
    try:
        rows = response.json()
    except ValueError as exc:
        raise ScheduledStoreError("Supabase returned invalid scheduled-discovery JSON.") from exc
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ScheduledStoreError("Supabase returned an invalid scheduled-discovery response.")
    return rows


def get_user_schedule(user_id: str, access_token: str, settings: Settings) -> tuple[bool, list[dict[str, Any]]]:
    key = settings.supabase_publishable_key or ""
    schedules = _request(
        settings, key, access_token, "GET", "discovery_schedules",
        params={"user_id": f"eq.{user_id}", "select": "user_id,enabled", "limit": "2"},
    )
    if len(schedules) > 1 or any(row.get("user_id") != user_id or not isinstance(row.get("enabled"), bool) for row in schedules):
        raise ScheduledStoreError("Supabase returned an invalid owner-scoped schedule.")
    runs = _request(
        settings, key, access_token, "GET", "agent_runs",
        params={
            "user_id": f"eq.{user_id}", "run_type": "eq.scheduled",
            "select": "id,status,started_at,finished_at,source_results,new_discoveries,updated_discoveries,error_summary,attempt_count",
            "order": "started_at.desc", "limit": "20",
        },
    )
    return (schedules[0]["enabled"] if schedules else False), runs


def set_user_schedule(user_id: str, enabled: bool, access_token: str, settings: Settings) -> bool:
    rows = _request(
        settings,
        settings.supabase_publishable_key or "",
        access_token,
        "POST",
        "discovery_schedules",
        params={"on_conflict": "user_id"},
        json={"user_id": user_id, "enabled": enabled},
        prefer="resolution=merge-duplicates,return=representation",
    )
    if len(rows) != 1 or rows[0].get("user_id") != user_id or rows[0].get("enabled") is not enabled:
        raise ScheduledStoreError("Supabase did not confirm the requested schedule setting.")
    return rows[0]["enabled"]


class ScheduledWorkerStore:
    """Minimal REST operations used by the cron process.

    The service-role credential is required only here and is never used by API routes.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        secret = settings.supabase_service_role_key
        self._service_key = secret.get_secret_value().strip() if secret else ""
        if not settings.supabase_url or not settings.supabase_publishable_key or not self._service_key:
            raise ScheduledStoreError(
                "Worker configuration is incomplete; set SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY, "
                "and SUPABASE_SERVICE_ROLE_KEY in the cron service environment."
            )

    def _request(self, method: str, resource: str, **kwargs: Any) -> list[dict[str, Any]]:
        return _request(self.settings, self._service_key, self._service_key, method, resource, **kwargs)

    def enabled_user_ids(self) -> list[str]:
        rows = self._request(
            "GET", "discovery_schedules",
            params={"enabled": "eq.true", "select": "user_id,enabled", "order": "user_id.asc"},
        )
        user_ids = [row.get("user_id") for row in rows]
        if any(not isinstance(user_id, str) or row.get("enabled") is not True for user_id, row in zip(user_ids, rows)):
            raise ScheduledStoreError("Supabase returned an invalid enabled schedule list.")
        return user_ids

    def get_pause_state(self, user_id: str) -> bool:
        rows = self._request(
            "GET", "agent_state",
            params={"user_id": f"eq.{user_id}", "select": "user_id,globally_paused", "limit": "2"},
        )
        if len(rows) != 1 or rows[0].get("user_id") != user_id or not isinstance(rows[0].get("globally_paused"), bool):
            raise ScheduledStoreError("Global Pause state could not be verified.")
        return rows[0]["globally_paused"]

    def claim_run(self, user_id: str, idempotency_key: str) -> dict[str, Any] | None:
        rows = self._request(
            "POST", "rpc/claim_scheduled_discovery_run",
            json={"p_user_id": user_id, "p_idempotency_key": idempotency_key, "p_lease_seconds": self.settings.scheduled_discovery_lease_seconds},
        )
        if len(rows) > 1:
            raise ScheduledStoreError("Supabase returned duplicate scheduled run claims.")
        return rows[0] if rows else None

    def persist_items(self, user_id: str, items: list[dict[str, Any]]) -> int:
        if not items:
            return 0
        payload = [{
            "user_id": user_id,
            "platform": item["platform"],
            "kind": "discovery",
            "source_identity": item["source_identity"],
            "source_url": item["source_url"],
            "body": item["description"],
            "status": "proposed",
            "metadata": {**item["metadata"], "title": item["title"], "ranking_score": item["ranking_score"], "ranking_reasons": item["ranking_reasons"]},
            "ranking_score": item["ranking_score"],
            "ranking_reasons": item["ranking_reasons"],
        } for item in items]
        inserted = self._request(
            "POST", "content_items",
            params={"on_conflict": "user_id,platform,source_identity"},
            json=payload,
            prefer="resolution=ignore-duplicates,return=representation",
        )
        return len(inserted)

    def finish_run(
        self,
        run_id: str,
        user_id: str,
        *,
        status: str,
        source_results: list[dict[str, Any]],
        new_discoveries: int,
        updated_discoveries: int,
        error_summary: str | None,
    ) -> None:
        rows = self._request(
            "PATCH", "agent_runs",
            params={"id": f"eq.{run_id}", "user_id": f"eq.{user_id}", "run_type": "eq.scheduled", "select": "id"},
            json={
                "status": status,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "source_results": source_results,
                "new_discoveries": new_discoveries,
                "updated_discoveries": updated_discoveries,
                "error_summary": error_summary,
                "lease_expires_at": None,
                "summary": {"source_results": source_results, "new_discoveries": new_discoveries, "updated_discoveries": updated_discoveries},
            },
            prefer="return=representation",
        )
        if len(rows) != 1 or rows[0].get("id") != run_id:
            raise ScheduledStoreError("Supabase did not confirm scheduled run completion.")
