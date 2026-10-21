import json
import sqlite3
from pathlib import Path

from backend.app import import_sqlite_state
from backend.app.config import Settings


USER = "11111111-1111-4111-8111-111111111111"


def make_legacy_db(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE agent_state (user_id TEXT PRIMARY KEY, paused INTEGER NOT NULL);
            CREATE TABLE action_policies (user_id TEXT, action_type TEXT, enabled INTEGER, requires_approval INTEGER);
            CREATE TABLE approvals (user_id TEXT, action_type TEXT, approved INTEGER);
            CREATE TABLE discovery_items (user_id TEXT, platform TEXT, source_identity TEXT, source_url TEXT,
                title TEXT, description TEXT, metadata TEXT, ranking_score REAL, ranking_reasons TEXT,
                status TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        """)
        connection.execute("INSERT INTO agent_state VALUES (?, 0)", (USER,))
        connection.execute("INSERT INTO action_policies VALUES (?, 'save_discovery', 1, 0)", (USER,))
        connection.execute("INSERT INTO approvals VALUES (?, 'public_comment', 1)", (USER,))
        connection.execute("INSERT INTO discovery_items VALUES (?, 'github', 'org/repo', 'https://github.com/org/repo', 'Repo', 'Description', ?, 4.0, ?, 'dismissed', CURRENT_TIMESTAMP)",
                           (USER, json.dumps({"topic": "ml"}), json.dumps(["matching topic"])))


def test_import_preview_is_read_only_and_preserves_dismissed_discovery(tmp_path):
    path = tmp_path / "legacy.db"
    make_legacy_db(path)
    before = path.read_bytes()
    data = import_sqlite_state.snapshot(f"sqlite:///{path.as_posix()}", USER)
    assert len(data["policies"]) == 1
    assert data["state"] == [{"user_id": USER, "paused": 0}]
    assert len(data["approvals"]) == 1
    assert data["discoveries"][0]["status"] == "dismissed"
    assert data["discoveries"][0]["metadata"] == {"topic": "ml"}
    assert path.read_bytes() == before


def test_import_apply_uses_verified_owner_and_never_overwrites_duplicate(monkeypatch):
    settings = Settings(agent_state_storage="supabase", supabase_url="https://project.supabase.co", supabase_publishable_key="sb_publishable_test")
    calls = []
    monkeypatch.setattr(import_sqlite_state, "get_settings", lambda: settings)
    class EmptyResponse:
        def json(self):
            return []
    def record(method, settings, token, **kwargs):
        calls.append((method, token, kwargs))
        return EmptyResponse()
    monkeypatch.setattr(import_sqlite_state.supabase_store, "_request", record)
    data = {"state": [{"user_id": USER, "paused": 0}], "policies": [{"action_type": "save_discovery", "enabled": True, "requires_approval": False}],
            "approvals": [], "discoveries": [{"platform": "github", "source_identity": "org/repo", "source_url": "https://github.com/org/repo", "title": "Repo", "description": "Text", "metadata": {}, "ranking_score": 1.0, "ranking_reasons": [], "status": "dismissed"}]}
    import_sqlite_state.import_snapshot(data, USER, "verified-user-token")
    assert [call[2]["resource"] for call in calls] == ["agent_state", "agent_state", "action_policies", "content_items"]
    assert all(call[1] == "verified-user-token" for call in calls)
    assert calls[3][2]["json"][0]["user_id"] == USER
    assert calls[3][2]["json"][0]["status"] == "dismissed"
    assert calls[1][2]["json"]["globally_paused"] is False
    assert calls[1][2]["headers"]["Prefer"] == "resolution=ignore-duplicates,return=representation"
    assert calls[2][2]["headers"]["Prefer"] == "resolution=ignore-duplicates,return=minimal"
    assert calls[3][2]["headers"]["Prefer"] == "resolution=ignore-duplicates,return=minimal"
