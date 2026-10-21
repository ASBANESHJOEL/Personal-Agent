import sqlite3
import json
import hashlib
from contextlib import closing
from pathlib import Path

from .config import get_settings


def _sqlite_path() -> str:
    url = get_settings().database_url
    if not url.startswith("sqlite:///"):
        # PostgreSQL is provisioned through Supabase migrations. Local API storage stays explicit.
        raise RuntimeError("This Milestone 1 API runtime currently requires a sqlite:/// DATABASE_URL")
    return url.removeprefix("sqlite:///")


def connect() -> sqlite3.Connection:
    path = _sqlite_path()
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with closing(connect()) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS agent_state (user_id TEXT PRIMARY KEY, paused INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        db.execute("CREATE TABLE IF NOT EXISTS action_policies (user_id TEXT NOT NULL, action_type TEXT NOT NULL, enabled INTEGER NOT NULL, requires_approval INTEGER NOT NULL, PRIMARY KEY (user_id, action_type))")
        db.execute("CREATE TABLE IF NOT EXISTS approvals (user_id TEXT NOT NULL, action_type TEXT NOT NULL, approved INTEGER NOT NULL, PRIMARY KEY (user_id, action_type))")
        db.execute("""CREATE TABLE IF NOT EXISTS discovery_items (
            user_id TEXT NOT NULL, platform TEXT NOT NULL, source_identity TEXT NOT NULL,
            source_url TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
            metadata TEXT NOT NULL DEFAULT '{}', ranking_score REAL NOT NULL,
            ranking_reasons TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'proposed'
                CHECK (status IN ('proposed','saved','dismissed')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, platform, source_identity)
        )""")


def ensure_user_state(user_id: str) -> None:
    ensure_policy_defaults(user_id)
    with closing(connect()) as db, db:
        db.execute("INSERT OR IGNORE INTO agent_state (user_id, paused) VALUES (?, 1)", (user_id,))


def ensure_policy_defaults(user_id: str) -> None:
    with closing(connect()) as db, db:
        db.executemany("INSERT OR IGNORE INTO action_policies VALUES (?, ?, ?, ?)", [(user_id, "save_discovery", 1, 0), (user_id, "draft_post", 1, 1), (user_id, "publish_post", 0, 1), (user_id, "public_comment", 0, 1), (user_id, "github_star", 0, 1)])


def get_sqlite_paused_optional(user_id: str) -> bool | None:
    """Read legacy SQLite state without creating or changing a row."""
    with closing(connect()) as connection:
        row = connection.execute("SELECT paused FROM agent_state WHERE user_id = ?", (user_id,)).fetchone()
    return bool(row["paused"]) if row else None


def get_paused(user_id: str) -> bool:
    with closing(connect()) as db:
        row = db.execute("SELECT paused FROM agent_state WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        raise RuntimeError("Global agent state is missing")
    return bool(row["paused"])


def set_paused(user_id: str, value: bool) -> None:
    with closing(connect()) as db, db:
        cursor = db.execute("UPDATE agent_state SET paused = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?", (int(value), user_id))
        if cursor.rowcount != 1:
            raise RuntimeError("Global agent state is missing")


def get_policy(user_id: str, action_type: str) -> tuple[bool, bool] | None:
    with closing(connect()) as db:
        row = db.execute("SELECT enabled, requires_approval FROM action_policies WHERE user_id = ? AND action_type = ?", (user_id, action_type)).fetchone()
    return (bool(row["enabled"]), bool(row["requires_approval"])) if row else None


def has_approval(user_id: str, action_type: str) -> bool:
    with closing(connect()) as db:
        row = db.execute("SELECT approved FROM approvals WHERE user_id = ? AND action_type = ?", (user_id, action_type)).fetchone()
    return bool(row["approved"]) if row else False


def record_approval(user_id: str, action_type: str, approved: bool = True) -> None:
    """Storage primitive for a future authenticated approval workflow; not exposed as an API yet."""
    with closing(connect()) as db, db:
        db.execute("INSERT INTO approvals (user_id, action_type, approved) VALUES (?, ?, ?) ON CONFLICT(user_id, action_type) DO UPDATE SET approved = excluded.approved", (user_id, action_type, int(approved)))


def upsert_discovery_items(user_id: str, items: list[dict]) -> None:
    with closing(connect()) as connection, connection:
        connection.executemany(
            """INSERT OR IGNORE INTO discovery_items
            (user_id, platform, source_identity, source_url, title, description, metadata, ranking_score, ranking_reasons)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(user_id, item["platform"], item["source_identity"], item["source_url"], item["title"], item["description"],
              json.dumps(item["metadata"]), item["ranking_score"], json.dumps(item["ranking_reasons"])) for item in items],
        )


def get_discovery_items(user_id: str, status: str | None = None) -> list[dict]:
    with closing(connect()) as connection:
        if status:
            rows = connection.execute("SELECT * FROM discovery_items WHERE user_id = ? AND status = ? ORDER BY ranking_score DESC, created_at DESC", (user_id, status)).fetchall()
        else:
            rows = connection.execute("SELECT * FROM discovery_items WHERE user_id = ? ORDER BY ranking_score DESC, created_at DESC", (user_id,)).fetchall()
    return [_discovery_row(row) for row in rows]


def set_discovery_status(user_id: str, item_id: str, status: str) -> dict:
    with closing(connect()) as connection, connection:
        rows = connection.execute("SELECT platform, source_identity FROM discovery_items WHERE user_id = ?", (user_id,)).fetchall()
        match = next(((row["platform"], row["source_identity"]) for row in rows if hashlib.sha256(f"{row['platform']}:{row['source_identity']}".encode()).hexdigest() == item_id), None)
        if match is None:
            raise LookupError("Discovery item was not found.")
        platform, identity = match
        cursor = connection.execute("UPDATE discovery_items SET status = ? WHERE user_id = ? AND platform = ? AND source_identity = ?", (status, user_id, platform, identity))
        if cursor.rowcount != 1:
            raise LookupError("Discovery item was not found.")
        row = connection.execute("SELECT * FROM discovery_items WHERE user_id = ? AND platform = ? AND source_identity = ?", (user_id, platform, identity)).fetchone()
    return _discovery_row(row)


def _discovery_row(row: sqlite3.Row) -> dict:
    return {"id": hashlib.sha256(f"{row['platform']}:{row['source_identity']}".encode()).hexdigest(), "platform": row["platform"], "kind": "repository" if row["platform"] == "github" else row["source_identity"].split(":", 1)[0],
            "source_identity": row["source_identity"], "source_url": row["source_url"], "title": row["title"], "description": row["description"],
            "metadata": json.loads(row["metadata"]), "ranking_score": row["ranking_score"], "ranking_reasons": json.loads(row["ranking_reasons"]),
            "status": row["status"], "created_at": row["created_at"]}
