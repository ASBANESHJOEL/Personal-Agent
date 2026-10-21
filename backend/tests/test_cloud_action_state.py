from pathlib import Path

import pytest

from backend.app import auth, policy, supabase_store
from backend.app.auth import Principal
from backend.app.config import Settings


USER = "11111111-1111-4111-8111-111111111111"
TOKEN = "verified-user-token"


class Response:
    def __init__(self, status=200, rows=None):
        self.status_code = status
        self.is_success = 200 <= status < 300
        self._rows = rows or []

    def json(self):
        return self._rows


class Postgrest:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        assert kwargs["headers"]["apikey"] == "sb_publishable_test"
        assert kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"
        resource = url.rsplit("/", 1)[-1]
        if method == "GET" and resource == "action_policies":
            return Response(rows=[{"user_id": USER, "action_type": "save_discovery", "enabled": True, "requires_approval": False}])
        if method == "GET" and resource == "action_queue":
            return Response(rows=[{"id": "action-one", "user_id": USER, "action_type": "public_comment", "status": "pending_approval"}])
        if method == "GET" and resource == "approvals":
            return Response(rows=[{"id": "approval-one", "user_id": USER, "action_id": "action-one", "decision": "approved"}])
        return Response()


@pytest.fixture
def cloud(monkeypatch):
    settings = Settings(agent_state_storage="supabase", supabase_url="https://project.supabase.co", supabase_publishable_key="sb_publishable_test")
    transport = Postgrest()
    monkeypatch.setattr(supabase_store.httpx, "request", transport.request)
    monkeypatch.setattr(policy, "get_settings", lambda: settings)
    return settings, transport


def test_policy_defaults_and_reads_use_verified_users_jwt_and_owner_filter(cloud):
    settings, transport = cloud
    supabase_store.ensure_policy_defaults(USER, TOKEN, settings)
    assert supabase_store.get_policy(USER, "save_discovery", TOKEN, settings) == (True, False)
    insert = transport.calls[0]
    assert insert[0] == "POST" and insert[1].endswith("/action_policies")
    assert insert[2]["headers"]["Prefer"] == "resolution=ignore-duplicates,return=minimal"
    read = transport.calls[1][2]
    assert read["params"]["user_id"] == f"eq.{USER}"


def test_cloud_approval_must_match_same_users_pending_action(cloud):
    settings, transport = cloud
    assert supabase_store.has_approval(USER, "public_comment", TOKEN, settings) is True
    queue_call = next(call for call in transport.calls if call[1].endswith("/action_queue"))
    approval_call = next(call for call in transport.calls if call[1].endswith("/approvals"))
    assert queue_call[2]["params"]["user_id"] == f"eq.{USER}"
    assert queue_call[2]["params"]["status"] == "eq.pending_approval"
    assert approval_call[2]["params"]["user_id"] == f"eq.{USER}"
    assert approval_call[2]["params"]["action_id"] == "eq.action-one"


def test_cloud_approval_response_with_foreign_owner_fails_closed(monkeypatch, cloud):
    settings, _transport = cloud
    def foreign(*_args, **kwargs):
        if kwargs.get("resource") == "action_queue":
            return Response(rows=[{"id": "foreign-action", "user_id": "22222222-2222-4222-8222-222222222222", "action_type": "public_comment", "status": "pending_approval"}])
        raise AssertionError("A foreign queue row must stop approval lookup")
    monkeypatch.setattr(supabase_store, "_request", foreign)
    with pytest.raises(supabase_store.CloudStoreError):
        supabase_store.has_approval(USER, "public_comment", TOKEN, settings)


def test_action_policy_auth_failure_is_not_treated_as_missing_policy(monkeypatch, cloud):
    settings, _transport = cloud
    monkeypatch.setattr(supabase_store.httpx, "request", lambda *_args, **_kwargs: Response(status=401))
    with pytest.raises(supabase_store.CloudStoreAuthError):
        supabase_store.get_policy(USER, "save_discovery", TOKEN, settings)


def test_action_policy_response_with_foreign_owner_fails_closed(monkeypatch, cloud):
    settings, _transport = cloud
    def foreign(*_args, **_kwargs):
        return Response(rows=[{"user_id": "22222222-2222-4222-8222-222222222222", "action_type": "save_discovery", "enabled": True, "requires_approval": False}])
    monkeypatch.setattr(supabase_store.httpx, "request", foreign)
    with pytest.raises(supabase_store.CloudStoreError):
        supabase_store.get_policy(USER, "save_discovery", TOKEN, settings)


def test_cloud_policy_gateway_fails_closed_without_user_token(monkeypatch, cloud):
    _settings, _transport = cloud
    decision = policy.authorize_action(Principal(USER), "save_discovery")
    assert decision.allowed is False
    assert decision.reason == "state_unavailable"


def test_production_rejects_sqlite_or_worker_key_on_api():
    assert Settings(app_env="production", agent_state_storage="sqlite").production_storage_error
    assert Settings(app_env="production", agent_state_storage="supabase", supabase_url="https://project.supabase.co", supabase_publishable_key="sb_publishable_test", supabase_service_role_key="service-test").production_storage_error
    assert Settings(app_env="production", agent_state_storage="supabase", supabase_url="https://project.supabase.co", supabase_publishable_key="sb_publishable_test").production_storage_error is None


def test_migration_revokes_inherited_grants_and_keeps_writes_owner_scoped():
    root = Path(__file__).resolve().parents[2]
    migration = root / "supabase/migrations/20261021053555_authenticated_agent_state_access.sql"
    sql = " ".join(migration.read_text(encoding="utf-8").lower().split())
    assert "revoke all privileges on table public.action_policies, public.action_queue, public.approvals, public.execution_attempts, public.audit_logs from public, anon, authenticated" in sql
    assert "grant insert (user_id, action_type, enabled, requires_approval, constraints)" in sql
    assert "create policy approvals_insert_own_action" in sql
    assert "q.id = action_id" in sql and "q.user_id = (select auth.uid())" in sql and "q.status = 'pending_approval'" in sql
    assert "grant insert" not in sql[sql.index("create policy approvals_insert_own_action"):]
    assert "grant delete" not in sql and "grant update" not in sql
    assert "service_role" not in sql
    assert not any(statement in sql for statement in ("drop table", "truncate", "delete from", "drop policy", "disable row level security"))
    initial = (root / "supabase/migrations/202610190001_initial_schema.sql").read_text(encoding="utf-8").lower()
    assert "table_name || '_select_own'" in initial and "auth.uid() = user_id" in initial
    for table in ("action_policies", "action_queue", "approvals", "execution_attempts", "audit_logs"):
        assert f"'{table}'" in initial
