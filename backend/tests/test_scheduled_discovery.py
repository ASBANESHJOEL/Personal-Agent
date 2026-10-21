from datetime import datetime, timezone
from pathlib import Path
import httpx
import pytest
from fastapi import HTTPException

from backend.app import db, discovery, schedule_api, scheduled_store
from backend.app.auth import Principal
from backend.app.config import Settings
from backend.app.discovery_worker import run_scheduled_batch


def cloud_settings(**overrides):
    return Settings(
        _env_file=None,
        agent_state_storage="supabase",
        supabase_url="https://project.supabase.co",
        supabase_publishable_key="sb_publishable_test",
        **overrides,
    )


def test_scheduling_is_disabled_by_default_and_settings_are_owner_scoped(monkeypatch):
    assert Settings(_env_file=None).scheduled_discovery_worker_deployed is False
    captured = []

    def fake_request(method, url, **kwargs):
        captured.append((method, url, kwargs))
        return httpx.Response(200, json=[])

    monkeypatch.setattr(scheduled_store.httpx, "request", fake_request)
    settings = cloud_settings()
    enabled, runs = scheduled_store.get_user_schedule("user-one", "verified-user-jwt", settings)
    assert enabled is False and runs == []
    assert all(call[2]["params"]["user_id"] == "eq.user-one" for call in captured)
    assert all(call[2]["headers"]["Authorization"] == "Bearer verified-user-jwt" for call in captured)


def test_schedule_update_derives_owner_from_verified_principal(monkeypatch):
    captured = []

    def set_schedule(user_id, enabled, access_token, settings):
        captured.append((user_id, enabled, access_token))
        return enabled

    def get_schedule(user_id, access_token, settings):
        captured.append((user_id, "read", access_token))
        return True, []

    monkeypatch.setattr(schedule_api.scheduled_store, "set_user_schedule", set_schedule)
    monkeypatch.setattr(schedule_api.scheduled_store, "get_user_schedule", get_schedule)
    principal = Principal("verified-owner", "verified-token")
    result = schedule_api.update_schedule(schedule_api.ScheduleUpdate(enabled=True), principal, cloud_settings())
    assert result.enabled is True
    assert captured == [("verified-owner", True, "verified-token"), ("verified-owner", "read", "verified-token")]
    with pytest.raises(HTTPException) as error:
        schedule_api.update_schedule(schedule_api.ScheduleUpdate(enabled=True), Principal("no-token"), cloud_settings())
    assert error.value.status_code == 401


class FakeWorkerStore:
    def __init__(self, users):
        self.users = users
        self.finished = []
        self.persisted = []
        self.claimed = set()
        self.pause_errors = set()
        self.persist_errors = set()
        self.coalesce = set()

    def enabled_user_ids(self):
        return self.users

    def claim_run(self, user_id, idempotency_key):
        assert idempotency_key == "scheduled-discovery:2026-10-20"
        if user_id in self.coalesce or user_id in self.claimed:
            return None
        self.claimed.add(user_id)
        return {"id": "run-" + user_id}

    def get_pause_state(self, user_id):
        if user_id in self.pause_errors:
            raise scheduled_store.ScheduledStoreError("sensitive transport detail")
        return self.users[user_id]

    def persist_items(self, user_id, items):
        self.persisted.append((user_id, items))
        if user_id in self.persist_errors:
            raise scheduled_store.ScheduledStoreError("sensitive storage detail")
        return len(items)

    def finish_run(self, run_id, user_id, **kwargs):
        self.finished.append((run_id, user_id, kwargs))


def test_worker_skips_paused_and_uncertain_state_then_processes_other_users(monkeypatch):
    store = FakeWorkerStore({"paused-user": True, "uncertain-user": False, "broken-user": False, "healthy-user": False})
    store.pause_errors.add("uncertain-user")
    store.persist_errors.add("broken-user")
    monkeypatch.setattr(discovery, "collect_discovery", lambda *_args: (
        [{"platform": "github", "source_identity": "owner/repo"}],
        [discovery.SourceResult(source="github", status="ok", count=1), discovery.SourceResult(source="huggingface:model", status="timeout", count=0)],
        "robotics",
    ))
    monkeypatch.setattr(db, "upsert_discovery_items", lambda *_args: pytest.fail("scheduled cloud failures must never fall back to SQLite"))

    result = run_scheduled_batch(
        cloud_settings(),
        store,
        datetime(2026, 10, 20, 3, 0, tzinfo=timezone.utc),
    )
    assert result.enabled_users == 4 and result.claimed == 4
    assert result.skipped == 2 and result.partially_failed == 1 and result.failed == 1
    assert result.succeeded == 0
    finished = {user_id: fields for _, user_id, fields in store.finished}
    assert finished["paused-user"]["status"] == "skipped"
    assert "Global Pause is on" in finished["paused-user"]["error_summary"]
    assert finished["uncertain-user"]["status"] == "skipped"
    assert "could not be verified" in finished["uncertain-user"]["error_summary"]
    assert finished["healthy-user"]["status"] == "partially_failed"
    assert finished["healthy-user"]["new_discoveries"] == 1
    assert finished["healthy-user"]["updated_discoveries"] == 0
    assert finished["broken-user"]["status"] == "failed"
    assert all("sensitive" not in (fields["error_summary"] or "") for fields in finished.values())


def test_worker_coalesces_duplicate_claims_and_records_success_counts(monkeypatch):
    store = FakeWorkerStore({"user-one": False, "user-two": False})
    store.coalesce.add("user-two")
    monkeypatch.setattr(discovery, "collect_discovery", lambda *_args: (
        [{"platform": "github"}, {"platform": "huggingface"}],
        [discovery.SourceResult(source="github", status="ok", count=1)],
        "robotics",
    ))
    result = run_scheduled_batch(cloud_settings(), store, datetime(2026, 10, 20, 3, 0, tzinfo=timezone.utc))
    assert result.succeeded == 1 and result.coalesced == 1
    assert len(store.finished) == 1
    assert store.finished[0][2]["status"] == "succeeded"
    assert store.finished[0][2]["new_discoveries"] == 2


def test_worker_requires_server_credential_without_leaking_it():
    secret = "must-never-appear-in-errors"
    settings = cloud_settings(supabase_service_role_key=None)
    with pytest.raises(scheduled_store.ScheduledStoreError) as error:
        scheduled_store.ScheduledWorkerStore(settings)
    assert "SUPABASE_SERVICE_ROLE_KEY" in str(error.value)
    assert secret not in str(error.value)
    configured = cloud_settings(supabase_service_role_key=secret)
    assert secret not in repr(configured)


def test_worker_insert_uses_conflict_ignore_to_preserve_saved_and_dismissed_rows(monkeypatch):
    captured = []

    def fake_request(method, url, **kwargs):
        captured.append((method, url, kwargs))
        return httpx.Response(201, json=[{"id": "inserted-only"}])

    monkeypatch.setattr(scheduled_store.httpx, "request", fake_request)
    settings = cloud_settings(supabase_service_role_key="service-role-test-key")
    store = scheduled_store.ScheduledWorkerStore(settings)
    item = {
        "platform": "github", "source_identity": "org/repo", "source_url": "https://github.com/org/repo",
        "description": "", "title": "org/repo", "metadata": {}, "ranking_score": 20,
        "ranking_reasons": [],
    }
    assert store.persist_items("owner-from-enabled-schedule", [item]) == 1
    _, url, kwargs = captured[0]
    assert url.endswith("/rest/v1/content_items")
    assert kwargs["headers"]["Prefer"] == "resolution=ignore-duplicates,return=representation"
    assert kwargs["params"]["on_conflict"] == "user_id,platform,source_identity"
    assert kwargs["json"][0]["status"] == "proposed"
    assert kwargs["json"][0]["user_id"] == "owner-from-enabled-schedule"


def test_scheduling_migration_has_owner_rls_and_atomic_lease_without_destructive_sql():
    migration = Path(__file__).resolve().parents[2] / "supabase" / "migrations" / "202610200003_scheduled_discovery.sql"
    sql = migration.read_text(encoding="utf-8").lower()
    assert "enabled boolean not null default false" in sql
    assert "auth.uid()) = user_id" in sql
    assert "claim_scheduled_discovery_run" in sql
    assert "pg_advisory_xact_lock" in sql
    assert "service_role" in sql
    assert "agent_runs_user_idempotency_key_unique" in sql
    assert "agent_runs_one_active_scheduled_per_user_unique" in sql
    assert "revoke all privileges on function public.claim_scheduled_discovery_run(uuid, text, integer) from public, anon, authenticated" in sql
    assert "grant execute on function public.claim_scheduled_discovery_run(uuid, text, integer) to service_role" in sql
    assert "create policy discovery_schedules_select_own" in sql
    assert "create policy discovery_schedules_insert_own" in sql
    assert "create policy discovery_schedules_update_own" in sql
    assert "grant delete" not in sql and "for delete" not in sql
    assert not any(statement in sql for statement in ("drop table", "truncate", "delete from", "drop policy", "drop trigger"))
