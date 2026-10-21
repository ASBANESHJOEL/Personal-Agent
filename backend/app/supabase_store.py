"""User-scoped agent state access through Supabase PostgREST and RLS."""
from __future__ import annotations

from typing import Any
import httpx

from .config import Settings, get_settings


class CloudStoreError(RuntimeError):
    status_code = 503


class CloudStoreAuthError(CloudStoreError):
    status_code = 401


class CloudStorePermissionError(CloudStoreError):
    status_code = 403


class CloudStoreSchemaError(CloudStoreError):
    pass


def readiness(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    if not settings.supabase_storage_configured:
        return {
            "status": "not_ready",
            "storage": "supabase-postgres",
            "configured": False,
            "verified": False,
            "detail": "SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY are required for Supabase state storage.",
        }
    # /ready has no user identity, so it must not probe agent_state: RLS correctly
    # denies anonymous reads. Configuration is reported separately from evidence
    # gathered by successful, authenticated state requests.
    return {
        "status": "configured",
        "storage": "supabase-postgres",
        "configured": True,
        "verified": False,
        "detail": "Supabase is configured; authenticated storage access has not yet been verified.",
    }


def _not_verified(detail: str, *, configured: bool = True) -> dict[str, Any]:
    return {"status": "not_ready", "storage": "supabase-postgres", "configured": configured, "verified": False, "detail": detail}


def get_paused(user_id: str, access_token: str, settings: Settings | None = None) -> bool:
    settings = _configured_settings(settings)
    _load_and_bootstrap(user_id, access_token, settings)
    row = _select_state(user_id, access_token, settings)
    if not row:
        raise CloudStoreError("Supabase agent state disappeared while it was being loaded.")
    return row[0]["globally_paused"]


def set_paused(user_id: str, access_token: str, paused: bool, settings: Settings | None = None) -> bool:
    settings = _configured_settings(settings)
    _load_and_bootstrap(user_id, access_token, settings)
    response = _request(
        "PATCH",
        settings,
        access_token,
        params={"user_id": f"eq.{user_id}", "select": "user_id,globally_paused,legacy_import_complete"},
        json={"globally_paused": paused, "legacy_import_complete": True},
        headers={"Prefer": "return=representation"},
    )
    rows = _rows(response)
    if len(rows) != 1 or not _valid_state_row(rows[0]) or rows[0]["globally_paused"] is not paused:
        raise CloudStoreError("Supabase did not confirm the requested Global Pause state.")
    return rows[0]["globally_paused"]


def _configured_settings(settings: Settings | None) -> Settings:
    settings = settings or get_settings()
    if not settings.supabase_storage_configured:
        raise CloudStoreError("SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY are required for Supabase state storage.")
    return settings


def _load_and_bootstrap(user_id: str, access_token: str, settings: Settings) -> None:
    if not access_token:
        raise CloudStoreAuthError("A verified Supabase access token is required for cloud state.")
    # A bounded retry handles concurrent first reads and the conditional one-time import.
    for _ in range(4):
        rows = _select_state(user_id, access_token, settings)
        if len(rows) > 1:
            raise CloudStoreError("Supabase returned more than one state row for this user.")
        if rows:
            row = rows[0]
            if not _valid_state_row(row):
                raise CloudStoreError("Supabase agent_state response did not match its schema.")
            if row["legacy_import_complete"]:
                return
            _request(
                "PATCH",
                settings,
                access_token,
                params={"user_id": f"eq.{user_id}", "legacy_import_complete": "eq.false", "select": "user_id"},
                json={"legacy_import_complete": True},
                headers={"Prefer": "return=representation"},
            )
            continue

        # A missing cloud row is different from an HTTP/auth/schema error. Initialize
        # it paused using only the authenticated cloud request. Local imports use the
        # explicit read-only-by-default migration utility instead of runtime fallback.
        _request(
            "POST",
            settings,
            access_token,
            params={"on_conflict": "user_id", "select": "user_id"},
            json={"user_id": user_id, "globally_paused": True, "legacy_import_complete": True},
            headers={"Prefer": "resolution=ignore-duplicates,return=representation"},
        )
    raise CloudStoreError("Supabase agent state initialization could not be confirmed.")


def _select_state(user_id: str, access_token: str, settings: Settings) -> list[dict[str, Any]]:
    response = _request(
        "GET",
        settings,
        access_token,
        params={"user_id": f"eq.{user_id}", "select": "user_id,globally_paused,legacy_import_complete", "limit": "2"},
    )
    return _rows(response)


def _request(
    method: str,
    settings: Settings,
    access_token: str,
    *,
    params: dict[str, str] | None = None,
    json: Any = None,
    headers: dict[str, str] | None = None,
    resource: str = "agent_state",
) -> httpx.Response:
    if not access_token:
        raise CloudStoreAuthError("A verified Supabase access token is required for cloud state.")
    request_headers = {
        "apikey": settings.supabase_publishable_key or "",
        # Forward the exact token verified by FastAPI. This makes PostgREST execute
        # as the authenticated user so auth.uid() and RLS apply.
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    if json is not None:
        request_headers["Content-Type"] = "application/json"
    request_headers.update(headers or {})
    try:
        response = httpx.request(
            method,
            f"{settings.supabase_url.rstrip('/')}/rest/v1/{resource}",
            params=params,
            json=json,
            headers=request_headers,
            timeout=settings.supabase_request_timeout_seconds,
        )
    except httpx.TimeoutException as exc:
        raise CloudStoreError("Supabase agent state request timed out.") from exc
    except httpx.RequestError as exc:
        raise CloudStoreError("Supabase agent state is unavailable.") from exc
    if response.status_code == 401:
        raise CloudStoreAuthError(f"Supabase rejected the authenticated user token for {resource}.")
    if response.status_code == 403:
        raise CloudStorePermissionError(f"Supabase denied access to this user's {resource}.")
    if response.status_code in (400, 404):
        try:
            error = response.json()
        except ValueError:
            error = {}
        code = error.get("code") if isinstance(error, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        if code in {"PGRST202", "PGRST204", "PGRST205"} or "schema cache" in str(message).lower():
            if resource == "agent_state":
                raise CloudStoreSchemaError("Supabase agent_state schema is missing or outdated. Apply the Personal Agent state migration.")
            raise CloudStoreSchemaError(f"Supabase {resource} schema is missing or outdated. Apply the Personal Agent migrations.")
    if not response.is_success:
        label = "agent state" if resource == "agent_state" else resource
        raise CloudStoreError(f"Supabase {label} request failed with HTTP {response.status_code}.")
    return response


DEFAULT_POLICIES = [
    {"action_type": "save_discovery", "enabled": True, "requires_approval": False},
    {"action_type": "draft_post", "enabled": True, "requires_approval": True},
    {"action_type": "publish_post", "enabled": False, "requires_approval": True},
    {"action_type": "public_comment", "enabled": False, "requires_approval": True},
    {"action_type": "github_star", "enabled": False, "requires_approval": True},
]


def ensure_policy_defaults(user_id: str, access_token: str, settings: Settings | None = None) -> None:
    """Insert conservative defaults under the user's JWT; never overwrite choices."""
    settings = _configured_settings(settings)
    payload = [{"user_id": user_id, **row} for row in DEFAULT_POLICIES]
    _request("POST", settings, access_token, params={"on_conflict": "user_id,action_type"}, json=payload,
             headers={"Prefer": "resolution=ignore-duplicates,return=minimal"}, resource="action_policies")


def get_policy(user_id: str, action_type: str, access_token: str, settings: Settings | None = None) -> tuple[bool, bool] | None:
    settings = _configured_settings(settings)
    response = _request("GET", settings, access_token,
                        params={"user_id": f"eq.{user_id}", "action_type": f"eq.{action_type}",
                                "select": "user_id,action_type,enabled,requires_approval", "limit": "2"},
                        resource="action_policies")
    rows = _rows(response)
    if len(rows) > 1 or any(row.get("user_id") != user_id or row.get("action_type") != action_type
                            or not isinstance(row.get("enabled"), bool)
                            or not isinstance(row.get("requires_approval"), bool) for row in rows):
        raise CloudStoreError("Supabase returned an invalid owner-scoped action policy.")
    return (rows[0]["enabled"], rows[0]["requires_approval"]) if rows else None


def has_approval(user_id: str, action_type: str, access_token: str, settings: Settings | None = None) -> bool:
    """An approval is valid only for this user's matching queued action."""
    settings = _configured_settings(settings)
    actions = _rows(_request("GET", settings, access_token,
                             params={"user_id": f"eq.{user_id}", "action_type": f"eq.{action_type}",
                                     "status": "eq.pending_approval", "select": "id,user_id,action_type,status", "limit": "20"},
                             resource="action_queue"))
    if any(not isinstance(row.get("id"), str) or row.get("user_id") != user_id
           or row.get("action_type") != action_type or row.get("status") != "pending_approval" for row in actions):
        raise CloudStoreError("Supabase returned an invalid owner-scoped action queue row.")
    for action in actions:
        rows = _rows(_request("GET", settings, access_token,
                              params={"user_id": f"eq.{user_id}", "action_id": f"eq.{action['id']}",
                                      "decision": "eq.approved", "select": "id,user_id,action_id,decision", "limit": "1"},
                              resource="approvals"))
        if rows:
            if any(row.get("user_id") != user_id or row.get("action_id") != action["id"]
                   or row.get("decision") != "approved" for row in rows):
                raise CloudStoreError("Supabase returned an invalid owner-scoped approval row.")
            return True
    return False


def _rows(response: httpx.Response) -> list[dict[str, Any]]:
    try:
        rows = response.json()
    except ValueError as exc:
        raise CloudStoreError("Supabase returned an invalid JSON response for agent_state.") from exc
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise CloudStoreError("Supabase returned an invalid agent_state response.")
    return rows


def _valid_state_row(row: dict[str, Any]) -> bool:
    return (
        isinstance(row.get("user_id"), str)
        and isinstance(row.get("globally_paused"), bool)
        and isinstance(row.get("legacy_import_complete"), bool)
    )


def upsert_discovery_items(user_id: str, access_token: str, items: list[dict[str, Any]], settings: Settings | None = None) -> list[dict[str, Any]]:
    settings = _configured_settings(settings)
    payload = [{"user_id": user_id, "platform": item["platform"], "kind": "discovery", "source_identity": item["source_identity"],
                "source_url": item["source_url"], "body": item["description"], "status": "proposed",
                "ranking_score": item["ranking_score"], "ranking_reasons": item["ranking_reasons"],
                "metadata": {**item["metadata"], "title": item["title"], "ranking_score": item["ranking_score"], "ranking_reasons": item["ranking_reasons"]}}
               for item in items]
    if payload:
        _request_content("POST", settings, access_token, params={"on_conflict": "user_id,platform,source_identity"}, json=payload,
                         headers={"Prefer": "resolution=ignore-duplicates,return=minimal"})
    return list_discovery_items(user_id, access_token, settings=settings)


def list_discovery_items(user_id: str, access_token: str, status: str | None = None, settings: Settings | None = None) -> list[dict[str, Any]]:
    settings = _configured_settings(settings)
    params = {"user_id": f"eq.{user_id}", "kind": "eq.discovery", "select": "id,user_id,platform,source_identity,source_url,body,status,metadata,ranking_score,ranking_reasons,created_at", "order": "created_at.desc", "limit": "500"}
    if status:
        params["status"] = f"eq.{status}"
    response = _request_content("GET", settings, access_token, params=params)
    rows = _rows(response)
    return [_discovery_row(row, user_id) for row in rows]


def update_discovery_status(user_id: str, access_token: str, item_id: str, status: str, settings: Settings | None = None) -> dict[str, Any]:
    settings = _configured_settings(settings)
    response = _request_content("PATCH", settings, access_token,
                                params={"id": f"eq.{item_id}", "user_id": f"eq.{user_id}", "kind": "eq.discovery", "select": "id,user_id,platform,source_identity,source_url,body,status,metadata,ranking_score,ranking_reasons,created_at"},
                                json={"status": status}, headers={"Prefer": "return=representation"})
    rows = _rows(response)
    if len(rows) != 1:
        raise CloudStoreError("Discovery item was not found for this user.")
    return _discovery_row(rows[0], user_id)


def _discovery_row(row: dict[str, Any], user_id: str) -> dict[str, Any]:
    if row.get("user_id") != user_id or row.get("platform") not in {"github", "huggingface"} or not isinstance(row.get("metadata"), dict):
        raise CloudStoreError("Supabase discovery response did not match its schema.")
    metadata = row["metadata"]
    identity = row.get("source_identity")
    if not isinstance(identity, str):
        raise CloudStoreError("Supabase discovery response did not match its schema.")
    return {"id": row["id"], "platform": row["platform"], "kind": (identity.split(":", 1)[0] if row["platform"] == "huggingface" else "repository"),
            "source_identity": identity, "source_url": row["source_url"], "title": metadata.get("title", identity),
            "description": row.get("body") or "", "metadata": {k: v for k, v in metadata.items() if k not in {"title", "ranking_score", "ranking_reasons"}},
            "ranking_score": row.get("ranking_score", metadata.get("ranking_score", 0)), "ranking_reasons": row.get("ranking_reasons", metadata.get("ranking_reasons", [])),
            "status": row.get("status"), "created_at": row.get("created_at")}


def _request_content(method: str, settings: Settings, access_token: str, *, params: dict[str, str], json: Any = None, headers: dict[str, str] | None = None) -> httpx.Response:
    if not access_token:
        raise CloudStoreAuthError("A verified Supabase access token is required for discovery storage.")
    request_headers = {"apikey": settings.supabase_publishable_key or "", "Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    if json is not None:
        request_headers["Content-Type"] = "application/json"
    request_headers.update(headers or {})
    try:
        response = httpx.request(method, f"{settings.supabase_url.rstrip('/')}/rest/v1/content_items", params=params, json=json,
                                 headers=request_headers, timeout=settings.supabase_request_timeout_seconds)
    except httpx.TimeoutException as exc:
        raise CloudStoreError("Supabase discovery storage request timed out.") from exc
    except httpx.RequestError as exc:
        raise CloudStoreError("Supabase discovery storage is unavailable.") from exc
    if response.status_code == 401:
        raise CloudStoreAuthError("Supabase rejected the authenticated user token.")
    if response.status_code == 403:
        raise CloudStorePermissionError("Supabase denied access to this user's discovery items.")
    if response.status_code in (400, 404):
        try:
            error = response.json()
        except ValueError:
            error = {}
        code = error.get("code") if isinstance(error, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        if code in {"PGRST202", "PGRST204", "PGRST205"} or "schema cache" in str(message).lower():
            raise CloudStoreSchemaError("Supabase discovery schema is missing or outdated. Apply the Personal Agent discovery migration.")
    if not response.is_success:
        raise CloudStoreError(f"Supabase discovery storage request failed with HTTP {response.status_code}.")
    return response
