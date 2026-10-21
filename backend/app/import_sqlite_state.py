"""Read-only preview and explicit, owner-scoped import of local app records.

Run from the backend directory. The SQLite source is opened with mode=ro and is
never changed. Existing SQLite approvals are reported but not imported because
the cloud schema requires an action_queue action_id and the legacy rows have no
such relationship.
"""
from __future__ import annotations

import argparse
import getpass
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from . import supabase_store
from .auth import verify_user_token
from .config import get_settings


def _readonly_connection(database_url: str) -> sqlite3.Connection:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise RuntimeError("The import source must be the configured local sqlite:/// DATABASE_URL.")
    raw_path = database_url.removeprefix(prefix)
    path = Path(raw_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve(strict=True)
    if not path.is_file():
        raise RuntimeError("The configured SQLite database file was not found.")
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def snapshot(database_url: str, user_id: str) -> dict[str, list[dict[str, Any]]]:
    with closing(_readonly_connection(database_url)) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        policies = []
        if "action_policies" in tables:
            policies = [dict(row) for row in connection.execute(
                "SELECT user_id, action_type, enabled, requires_approval FROM action_policies WHERE user_id=? ORDER BY action_type", (user_id,)
            )]
        approvals = []
        if "approvals" in tables:
            approvals = [dict(row) for row in connection.execute(
                "SELECT user_id, action_type, approved FROM approvals WHERE user_id=? ORDER BY action_type", (user_id,)
            )]
        state = []
        if "agent_state" in tables:
            state = [dict(row) for row in connection.execute(
                "SELECT user_id, paused FROM agent_state WHERE user_id=? LIMIT 2", (user_id,)
            )]
            if len(state) > 1 or any(row.get("paused") not in (0, 1) for row in state):
                raise RuntimeError("The local Global Pause record is invalid; no records were imported.")
        discoveries = []
        if "discovery_items" in tables:
            discoveries = [dict(row) for row in connection.execute(
                "SELECT platform, source_identity, source_url, title, description, metadata, ranking_score, ranking_reasons, status "
                "FROM discovery_items WHERE user_id=? ORDER BY platform, source_identity", (user_id,)
            )]
    for row in discoveries:
        row["metadata"] = json.loads(row["metadata"] or "{}")
        row["ranking_reasons"] = json.loads(row["ranking_reasons"] or "[]")
        if row["status"] not in {"proposed", "saved", "dismissed"}:
            raise RuntimeError("A local discovery has an unsupported status; no records were imported.")
    return {"state": state, "policies": policies, "approvals": approvals, "discoveries": discoveries}


def import_snapshot(data: dict[str, list[dict[str, Any]]], user_id: str, access_token: str) -> bool:
    settings = get_settings()
    supabase_store._configured_settings(settings)
    existing = supabase_store._rows(supabase_store._request(
        "GET", settings, access_token,
        params={"user_id": f"eq.{user_id}", "select": "user_id,globally_paused,legacy_import_complete", "limit": "2"},
        resource="agent_state",
    ))
    if len(existing) > 1 or any(row.get("user_id") != user_id for row in existing):
        raise RuntimeError("Supabase returned an invalid owner-scoped Global Pause row.")
    pause_imported = False
    if existing:
        row = existing[0]
        if not isinstance(row.get("globally_paused"), bool) or not isinstance(row.get("legacy_import_complete"), bool):
            raise RuntimeError("Supabase Global Pause row did not match its schema.")
        if not row["legacy_import_complete"]:
            patch = {"legacy_import_complete": True}
            if data["state"]:
                patch["globally_paused"] = bool(data["state"][0]["paused"])
            rows = supabase_store._rows(supabase_store._request("PATCH", settings, access_token, resource="agent_state",
                                    params={"user_id": f"eq.{user_id}", "legacy_import_complete": "eq.false", "select": "user_id"},
                                    json=patch, headers={"Prefer": "return=representation"}))
            pause_imported = len(rows) == 1 and rows[0].get("user_id") == user_id
    else:
        rows = supabase_store._rows(supabase_store._request("POST", settings, access_token, resource="agent_state",
                                params={"on_conflict": "user_id"},
                                json={"user_id": user_id,
                                      "globally_paused": bool(data["state"][0]["paused"]) if data["state"] else True,
                                      "legacy_import_complete": True},
                                headers={"Prefer": "resolution=ignore-duplicates,return=representation"}))
        pause_imported = len(rows) == 1 and rows[0].get("user_id") == user_id
    policies = [{"user_id": user_id, "action_type": row["action_type"], "enabled": bool(row["enabled"]),
                 "requires_approval": bool(row["requires_approval"])} for row in data["policies"]]
    if policies:
        supabase_store._request("POST", settings, access_token, resource="action_policies",
                                params={"on_conflict": "user_id,action_type"}, json=policies,
                                headers={"Prefer": "resolution=ignore-duplicates,return=minimal"})
    discoveries = [{
        "user_id": user_id, "platform": row["platform"], "kind": "discovery",
        "source_identity": row["source_identity"], "source_url": row["source_url"],
        "body": row["description"], "status": row["status"],
        "metadata": {**row["metadata"], "title": row["title"]},
        "ranking_score": row["ranking_score"], "ranking_reasons": row["ranking_reasons"],
    } for row in data["discoveries"]]
    if discoveries:
        supabase_store._request("POST", settings, access_token, resource="content_items",
                                params={"on_conflict": "user_id,platform,source_identity"}, json=discoveries,
                                headers={"Prefer": "resolution=ignore-duplicates,return=minimal"})
    return pause_imported


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True, help="Supabase Auth UUID to preview/import; apply verifies it against the JWT.")
    parser.add_argument("--apply", action="store_true", help="Apply import. Without this flag the command only performs a dry run.")
    args = parser.parse_args()
    settings = get_settings()
    if settings.agent_state_storage != "supabase" or not settings.supabase_storage_configured:
        raise SystemExit("Set AGENT_STATE_STORAGE=supabase, SUPABASE_URL, and SUPABASE_PUBLISHABLE_KEY before importing.")
    data = snapshot(settings.database_url, args.user_id)
    print(f"Dry-run preview for user {args.user_id}: {len(data['state'])} Global Pause rows, "
          f"{len(data['policies'])} policies, "
          f"{len(data['discoveries'])} discoveries, {len(data['approvals'])} legacy approvals.")
    if data["approvals"]:
        print("Legacy approvals will be skipped: they lack the cloud action_queue action_id; re-approve through a supported workflow.")
    if not args.apply:
        print("No writes performed. Re-run with --apply to import using a verified Supabase user token.")
        return 0
    token = getpass.getpass("Supabase user access token (input hidden): ")
    try:
        principal = verify_user_token(token, settings)
    except HTTPException as exc:
        raise SystemExit(f"Access token verification failed with HTTP {exc.status_code}; no writes performed.") from exc
    if principal.user_id != args.user_id:
        raise SystemExit("The token owner does not match --user-id; no writes performed.")
    # Take a fresh read-only snapshot only after verifying the signed-in owner.
    data = snapshot(settings.database_url, principal.user_id)
    pause_imported = import_snapshot(data, principal.user_id, token)
    pause_result = "imported/initialized" if pause_imported else "already finalized in Supabase; left unchanged"
    print(f"Global Pause: {pause_result}. Imported {len(data['policies'])} policies and {len(data['discoveries'])} discoveries. "
          "The original SQLite database was not modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
