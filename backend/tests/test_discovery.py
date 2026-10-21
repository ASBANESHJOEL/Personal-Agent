from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from backend.app import db, discovery, supabase_store
from backend.app.auth import Principal
from backend.app.config import Settings


def test_ranking_is_deterministic_and_explains_matches():
    item = {"platform": "github", "title": "robotics computer vision", "description": "reproducible experiment",
            "metadata": {"topics": ["robotics", "computer-vision"], "stars": 42,
                         "updated_at": "2026-10-19T00:00:00Z"}}
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    score, reasons = discovery.rank_item(item, "robotics vision", now)
    assert score == discovery.rank_item(item, "robotics vision", now)[0]
    assert score > 50
    assert any("official public provider" in reason for reason in reasons)
    assert any("robotics" in reason for reason in reasons)
    assert any("search" in reason for reason in reasons)


def test_provider_rate_limit_is_classified_without_exposing_response_body():
    class RateLimitedClient:
        def get(self, *_args, **_kwargs):
            return httpx.Response(429, json={"token": "sensitive-provider-body"})

    with pytest.raises(discovery._RateLimited):
        discovery._fetch_json(RateLimitedClient(), "https://api.github.com/search/repositories", params={}, headers={})
    status = discovery._source_status(discovery._RateLimited())
    assert status.status == "rate_limited"
    assert "sensitive-provider-body" not in (status.detail or "")


def test_provider_parsers_reject_external_links_and_bound_untrusted_text():
    assert discovery._github_item({"full_name": "bad/repo", "html_url": "https://github.com.evil.invalid/bad/repo"}) is None
    item = discovery._github_item({"full_name": "valid/repo", "html_url": "https://github.com/valid/repo",
                                   "description": "safe\u0000 <script>alert(1)</script>"})
    assert item is not None
    assert item["description"] == "safe <script>alert(1)</script>"
    assert discovery.GITHUB_TOPIC_QUERY.count(" OR ") <= 5


def test_run_handles_partial_provider_failure_and_persists_idempotently(monkeypatch, client):
    principal = Principal("discover-user")
    calls = []

    def fetch(_client, url, *, params, headers):
        calls.append((url, params, headers))
        if "api.github.com" in url:
            return {"items": [{"full_name": "open-cv/robotics", "html_url": "https://github.com/open-cv/robotics",
                               "description": "Computer vision and robotics", "topics": ["robotics", "vision"],
                               "stargazers_count": 12, "updated_at": "2026-10-19T00:00:00Z"}]}
        if "api/models" in url:
            return [{"id": "sample/vision-model", "tags": ["computer-vision"], "downloads": 123, "lastModified": "2026-10-18T00:00:00Z"}]
        if "api/datasets" in url:
            raise httpx.TimeoutException("test timeout")
        raise discovery._RateLimited()

    monkeypatch.setattr(discovery, "_fetch_json", fetch)
    request = discovery.DiscoveryRunRequest(query="robotics vision", limit=10, page=2)
    first = discovery.run_discovery(Principal("discover-user"), request, Settings(discovery_request_timeout_seconds=2))
    assert len(first.items) == 2
    assert first.sources[0].status == "ok"
    assert any(source.status == "timeout" for source in first.sources)
    assert any(source.status == "rate_limited" for source in first.sources)
    assert all(item.ranking_reasons for item in first.items)
    assert any(params.get("page") == 2 for url, params, _ in calls if "api.github.com" in url)
    assert any(params.get("offset") == 10 for url, params, _ in calls if "huggingface.co" in url)
    item_id = first.items[0].id
    other_item_id = next(item.id for item in first.items if item.id != item_id)
    dismissed = discovery.set_item_status(Principal("discover-user"), item_id, "dismissed", Settings(agent_state_storage="sqlite"))
    saved = discovery.set_item_status(Principal("discover-user"), other_item_id, "saved", Settings(agent_state_storage="sqlite"))
    assert dismissed["status"] == "dismissed"
    assert saved["status"] == "saved"
    repeated = discovery.run_discovery(Principal("discover-user"), request, Settings(discovery_request_timeout_seconds=2))
    assert len(repeated.items) == 2
    assert next(item for item in repeated.items if item.id == item_id).status == "dismissed"
    assert next(item for item in repeated.items if item.id == other_item_id).status == "saved"
    assert len(discovery.list_items(Principal("discover-user"), settings=Settings(agent_state_storage="sqlite"))) == 2


def test_discovery_rows_and_status_are_scoped_to_principal(client):
    first = Principal("owner-one")
    second = Principal("owner-two")
    item = {"platform": "github", "source_identity": "owner/repo", "source_url": "https://github.com/owner/repo",
            "title": "Owner Repo", "description": "metadata", "metadata": {"stars": 1},
            "ranking_score": 20.0, "ranking_reasons": ["metadata"]}
    discovery.persist_items(first, [item], Settings(agent_state_storage="sqlite"))
    owner_row = discovery.list_items(first, settings=Settings(agent_state_storage="sqlite"))[0]
    assert discovery.list_items(second, settings=Settings(agent_state_storage="sqlite")) == []
    with pytest.raises(LookupError):
        discovery.set_item_status(second, owner_row["id"], "saved", Settings(agent_state_storage="sqlite"))
    assert discovery.set_item_status(first, owner_row["id"], "dismissed", Settings(agent_state_storage="sqlite"))["status"] == "dismissed"


def test_supabase_discovery_uses_verified_user_token_and_owner_filters(monkeypatch):
    captured = []

    def fake_request(method, url, **kwargs):
        captured.append((method, url, kwargs))
        return httpx.Response(200, json=[])

    monkeypatch.setattr(supabase_store.httpx, "request", fake_request)
    settings = Settings(agent_state_storage="supabase", supabase_url="https://project.supabase.co", supabase_publishable_key="sb_publishable_test")
    assert supabase_store.list_discovery_items("owner-id", "verified-token", settings=settings) == []
    method, url, kwargs = captured[0]
    assert method == "GET" and url.endswith("/rest/v1/content_items")
    assert kwargs["headers"]["Authorization"] == "Bearer verified-token"
    assert kwargs["headers"]["apikey"] == "sb_publishable_test"
    assert kwargs["params"]["user_id"] == "eq.owner-id"
    assert kwargs["params"]["kind"] == "eq.discovery"


def test_supabase_upsert_uses_ignore_duplicates_and_preserves_status(monkeypatch):
    captured = []

    def fake_request(method, url, **kwargs):
        captured.append((method, url, kwargs))
        return httpx.Response(201, json=[])

    monkeypatch.setattr(supabase_store.httpx, "request", fake_request)
    item = {"platform": "github", "source_identity": "owner/repo", "source_url": "https://github.com/owner/repo",
            "title": "Owner Repo", "description": "", "metadata": {"topics": ["robotics"]},
            "ranking_score": 80, "ranking_reasons": ["topic match"]}
    settings = Settings(agent_state_storage="supabase", supabase_url="https://project.supabase.co", supabase_publishable_key="sb_publishable_test")
    assert supabase_store.upsert_discovery_items("owner-id", "verified-token", [item], settings) == []
    method, _, kwargs = captured[0]
    assert method == "POST"
    assert kwargs["headers"]["Prefer"] == "resolution=ignore-duplicates,return=minimal"
    assert kwargs["headers"]["Authorization"] == "Bearer verified-token"
    assert kwargs["params"]["on_conflict"] == "user_id,platform,source_identity"
    assert kwargs["json"][0]["user_id"] == "owner-id"
    assert kwargs["json"][0]["status"] == "proposed"
    assert captured[1][0] == "GET"


def test_discovery_migration_adds_only_owner_scoped_insert_update_access():
    migration = Path(__file__).resolve().parents[2] / "supabase" / "migrations" / "202610200002_discovery_content_items.sql"
    sql = migration.read_text(encoding="utf-8").lower()
    assert "add column if not exists source_identity text" in sql
    assert "create unique index if not exists content_items_discovery_identity_unique" in sql
    revoke = "revoke all privileges on table public.content_items from public, anon, authenticated"
    assert revoke in sql
    assert sql.index(revoke) < sql.index("grant select on table public.content_items to authenticated")
    assert "grant select on table public.content_items to authenticated" in sql
    assert "grant insert (user_id, platform, kind, source_identity, source_url, body, status, metadata, ranking_score, ranking_reasons)" in sql
    assert "grant update (status) on table public.content_items to authenticated" in sql
    assert "grant delete" not in sql
    assert "for delete" not in sql
    assert "create policy content_items_insert_own" in sql and "kind = 'discovery'" in sql
    assert "with check ((select auth.uid()) = user_id and kind = 'discovery' and status in ('proposed','saved','dismissed'))" in sql
    assert "create policy content_items_update_own" in sql
    assert "using ((select auth.uid()) = user_id and kind = 'discovery' and status in ('proposed','saved','dismissed'))" in sql
    assert "with check ((select auth.uid()) = user_id and kind = 'discovery' and status in ('proposed','saved','dismissed'))" in sql
    assert not any(statement in sql for statement in ("drop table", "truncate", "delete from", "drop policy", "drop trigger"))
