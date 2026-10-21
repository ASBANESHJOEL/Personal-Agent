from __future__ import annotations

from contextlib import closing
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from backend.app import db, main, policy, supabase_store
from backend.app.auth import Principal
from backend.app.config import Settings
from backend.app.policy import authorize_action


USER_ONE = "11111111-1111-4111-8111-111111111111"
USER_TWO = "22222222-2222-4222-8222-222222222222"
ACCESS_TOKEN = "verified-user-access-token"
USER_TWO_TOKEN = "verified-second-user-access-token"


class Response:
    def __init__(self, status_code: int = 200, body=None, *, code: str | None = None, message: str | None = None):
        self.status_code = status_code
        self._body = body
        self.is_success = 200 <= status_code < 300
        self.code = code
        self.message = message

    def json(self):
        if self.code or self.message:
            return {"code": self.code, "message": self.message}
        return self._body


class MockCloud:
    """Tiny PostgREST/RLS model that persists rows between independent calls."""

    def __init__(self, rows=None, access_token=ACCESS_TOKEN):
        self.rows = rows or {}
        self.access_token = access_token
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        headers = kwargs["headers"]
        assert headers["apikey"] == "sb_publishable_test"
        assert headers["Authorization"] == f"Bearer {self.access_token}"
        params = kwargs.get("params") or {}
        user_id = params.get("user_id", "").removeprefix("eq.")
        resource = url.rsplit("/", 1)[-1]
        if resource == "action_policies" and method == "GET":
            return Response(body=[{"user_id": user_id, "action_type": params.get("action_type", "").removeprefix("eq."), "enabled": True, "requires_approval": False}])
        if resource in {"action_queue", "approvals"} and method == "GET":
            return Response(body=[])
        if method == "GET":
            row = self.rows.get(user_id)
            return Response(body=[dict(row)] if row else [])
        if method == "POST":
            body = kwargs["json"]
            # The backend derives this owner from the already-verified token claims.
            self.rows.setdefault(body["user_id"], {**body})
            return Response(body=[])
        if method == "PATCH":
            row = self.rows.get(user_id)
            if row and params.get("legacy_import_complete") == "eq.false" and row["legacy_import_complete"]:
                return Response(body=[])
            if row:
                row.update(kwargs["json"])
                return Response(body=[dict(row)])
            return Response(body=[])
        raise AssertionError(f"Unexpected HTTP method {method}")


@pytest.fixture
def cloud(monkeypatch, client):
    settings = Settings(
        agent_state_storage="supabase",
        supabase_url="https://project.supabase.co",
        supabase_publishable_key="sb_publishable_test",
        supabase_request_timeout_seconds=0.5,
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(policy, "get_settings", lambda: settings)
    monkeypatch.setattr(supabase_store, "get_settings", lambda: settings)
    with closing(db.connect()) as connection, connection:
        connection.execute("DELETE FROM agent_state WHERE user_id IN (?, ?)", (USER_ONE, USER_TWO))
        connection.execute("DELETE FROM action_policies WHERE user_id IN (?, ?)", (USER_ONE, USER_TWO))
    db.ensure_policy_defaults(USER_ONE)
    principal = Principal(USER_ONE, ACCESS_TOKEN)
    return settings, principal


def test_cloud_state_is_read_with_verified_bearer_and_rls_owner_filter(monkeypatch, cloud):
    _settings, _principal = cloud
    store = MockCloud({USER_ONE: {"user_id": USER_ONE, "globally_paused": False, "legacy_import_complete": True}})
    monkeypatch.setattr(supabase_store.httpx, "request", store.request)

    assert supabase_store.get_paused(USER_ONE, ACCESS_TOKEN) is False
    method, url, call = store.calls[0]
    assert method == "GET"
    assert url == "https://project.supabase.co/rest/v1/agent_state"
    assert call["params"]["user_id"] == f"eq.{USER_ONE}"
    assert call["headers"]["Authorization"] == f"Bearer {ACCESS_TOKEN}"


def test_cloud_state_bootstrap_does_not_read_local_sqlite_or_overwrite_cloud_pause(monkeypatch, cloud):
    _settings, principal = cloud
    db.ensure_user_state(USER_ONE)
    db.set_paused(USER_ONE, True)
    store = MockCloud({USER_ONE: {"user_id": USER_ONE, "globally_paused": False, "legacy_import_complete": False}})
    monkeypatch.setattr(supabase_store.httpx, "request", store.request)
    monkeypatch.setattr(db, "get_sqlite_paused_optional", lambda _user: (_ for _ in ()).throw(AssertionError("cloud runtime must not read SQLite")))

    assert main.state(principal) == {"paused": False}
    assert store.rows[USER_ONE] == {"user_id": USER_ONE, "globally_paused": False, "legacy_import_complete": True}
    assert any(method == "PATCH" and call["json"] == {"legacy_import_complete": True} for method, _, call in store.calls)

    # Cloud state remains authoritative on subsequent requests.
    assert supabase_store.get_paused(USER_ONE, ACCESS_TOKEN) is False


def test_missing_cloud_state_initializes_paused_when_no_legacy_row(monkeypatch, cloud):
    _settings, _principal = cloud
    store = MockCloud()
    monkeypatch.setattr(supabase_store.httpx, "request", store.request)

    assert supabase_store.get_paused(USER_ONE, ACCESS_TOKEN) is True
    assert store.rows[USER_ONE]["globally_paused"] is True
    assert store.rows[USER_ONE]["legacy_import_complete"] is True


def test_pause_and_resume_are_confirmed_by_cloud_and_survive_repository_recreation(monkeypatch, cloud):
    _settings, principal = cloud
    store = MockCloud()
    monkeypatch.setattr(supabase_store.httpx, "request", store.request)

    assert main.state(principal) == {"paused": True}
    assert main.resume(principal) == {"paused": False}
    assert main.pause(principal) == {"paused": True}
    # Reads are made through a fresh repository invocation and persisted server state.
    assert supabase_store.get_paused(USER_ONE, ACCESS_TOKEN) is True
    assert [method for method, _, _ in store.calls].count("PATCH") == 2


def test_cloud_rows_are_isolated_by_user_filter(monkeypatch, cloud):
    _settings, _principal = cloud
    second_user_store = MockCloud({USER_ONE: {"user_id": USER_ONE, "globally_paused": False, "legacy_import_complete": True}}, access_token=USER_TWO_TOKEN)
    monkeypatch.setattr(supabase_store.httpx, "request", second_user_store.request)
    assert supabase_store.get_paused(USER_TWO, USER_TWO_TOKEN) is True
    assert second_user_store.rows[USER_ONE]["globally_paused"] is False
    assert second_user_store.rows[USER_TWO]["globally_paused"] is True
    reads = [call for method, _, call in second_user_store.calls if method == "GET"]
    inserts = [call for method, _, call in second_user_store.calls if method == "POST"]
    assert reads and all(call["params"].get("user_id") == f"eq.{USER_TWO}" for call in reads)
    assert inserts and all(call["json"]["user_id"] == USER_TWO for call in inserts)


@pytest.mark.parametrize("status,expected", [(401, 401), (403, 403)])
def test_cloud_authorization_errors_are_not_treated_as_missing_rows(monkeypatch, cloud, status, expected):
    _settings, principal = cloud

    def reject(*_args, **_kwargs):
        return Response(status, code="42501", message="permission denied")

    monkeypatch.setattr(supabase_store.httpx, "request", reject)
    with pytest.raises(HTTPException) as error:
        main.state(principal)
    assert error.value.status_code == expected


def test_genuine_cloud_errors_fail_closed_as_unavailable(monkeypatch, cloud):
    _settings, principal = cloud
    monkeypatch.setattr(
        supabase_store.httpx,
        "request",
        lambda *_args, **_kwargs: Response(500, code="XX000", message="database unavailable"),
    )

    with pytest.raises(HTTPException) as error:
        main.state(principal)
    assert error.value.status_code == 503
    assert error.value.detail == "Supabase agent state request failed with HTTP 500."


def test_invalid_cloud_state_never_defaults_to_unpaused(monkeypatch, cloud):
    _settings, principal = cloud
    malformed = MockCloud({USER_ONE: {
        "user_id": USER_ONE,
        "globally_paused": False,
        "legacy_import_complete": "uncertain",
    }})
    monkeypatch.setattr(supabase_store.httpx, "request", malformed.request)

    with pytest.raises(HTTPException) as error:
        main.state(principal)
    assert error.value.status_code == 503
    assert error.value.detail == "Supabase agent_state response did not match its schema."
    assert authorize_action(principal, "save_discovery").reason == "state_unavailable"


def test_schema_missing_cloud_timeout_and_outage_fail_closed(monkeypatch, cloud):
    _settings, principal = cloud

    def schema_missing(*_args, **_kwargs):
        return Response(404, code="PGRST205", message="table not found in schema cache")

    monkeypatch.setattr(supabase_store.httpx, "request", schema_missing)
    with pytest.raises(HTTPException) as missing_schema:
        main.state(principal)
    assert missing_schema.value.status_code == 503
    assert "migration" in missing_schema.value.detail.lower()

    def timeout(*_args, **_kwargs):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(supabase_store.httpx, "request", timeout)
    with pytest.raises(HTTPException) as unavailable:
        main.resume(principal)
    assert unavailable.value.status_code == 503
    assert unavailable.value.detail == "Supabase agent state request timed out."
    assert authorize_action(principal, "save_discovery").reason == "state_unavailable"


def test_policy_gateway_uses_cloud_pause_and_blocks_uncertain_state(monkeypatch, cloud):
    _settings, principal = cloud
    store = MockCloud({USER_ONE: {"user_id": USER_ONE, "globally_paused": True, "legacy_import_complete": True}})
    monkeypatch.setattr(supabase_store.httpx, "request", store.request)
    assert authorize_action(principal, "save_discovery").reason == "agent_paused"

    monkeypatch.setattr(supabase_store.httpx, "request", lambda *_args, **_kwargs: (_ for _ in ()).throw(httpx.ConnectError("offline")))
    assert authorize_action(principal, "save_discovery").reason == "state_unavailable"


def test_forward_migration_grants_only_owner_scoped_authenticated_state_access():
    migration = Path(__file__).resolve().parents[2] / "supabase" / "migrations" / "202610190002_cloud_agent_state.sql"
    sql = migration.read_text(encoding="utf-8").lower()
    assert "legacy_import_complete" in sql
    assert "grant select, insert, update" in sql
    assert "revoke all on table public.agent_state from anon, public" in sql
    assert sql.count("(select auth.uid()) = user_id") == 4


def test_initializer_privileges_are_restricted_without_changing_trigger_or_rls():
    root = Path(__file__).resolve().parents[2]
    migration = root / "supabase" / "migrations" / "202610200001_restrict_state_initializer.sql"
    sql = " ".join(migration.read_text(encoding="utf-8").lower().split())
    initial_schema = (root / "supabase" / "migrations" / "202610190001_initial_schema.sql").read_text(encoding="utf-8").lower()

    assert "revoke execute on function public.initialize_personal_agent_state() from public, anon, authenticated;" in sql
    assert "create trigger on_auth_user_created_personal_agent after insert on auth.users" in " ".join(initial_schema.split())
    assert "language plpgsql security definer set search_path = ''" in " ".join(initial_schema.split())
    assert "initialize_personal_agent_state()" in initial_schema
    for destructive_or_unrelated in ("drop table", "truncate", "delete from", "drop trigger", "create trigger", "create policy", "alter table"):
        assert destructive_or_unrelated not in sql
    assert "create function" not in sql and "create or replace function" not in sql
