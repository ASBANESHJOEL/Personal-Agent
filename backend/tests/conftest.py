import pytest
from contextlib import closing

from backend.app import db
from backend.app.auth import Principal, require_user
from backend.app.main import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///.test_personal_agent_v3.sqlite3")
    monkeypatch.setenv("AGENT_STATE_STORAGE", "sqlite")
    db.get_settings.cache_clear()
    db.init_db()
    with closing(db.connect()) as connection, connection:
        connection.execute("DELETE FROM approvals")
    db.ensure_user_state("test-user")
    db.set_paused("test-user", True)
    with closing(db.connect()) as connection, connection:
        connection.execute("UPDATE action_policies SET enabled = 0, requires_approval = 1 WHERE user_id = 'test-user' AND action_type IN ('publish_post', 'public_comment', 'github_star')")
        connection.execute("UPDATE action_policies SET enabled = 1, requires_approval = 0 WHERE user_id = 'test-user' AND action_type = 'save_discovery'")
    app.dependency_overrides[require_user] = lambda: Principal("test-user")
    yield app
    app.dependency_overrides.clear()
    db.get_settings.cache_clear()
