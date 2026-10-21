"""Opt-in check against a real Supabase project (never runs by default).

Set PERSONAL_AGENT_RUN_LIVE_SUPABASE_TESTS=1 and provide SUPABASE_URL,
SUPABASE_PUBLISHABLE_KEY, and PERSONAL_AGENT_LIVE_SUPABASE_ACCESS_TOKEN in the
process environment. The token must belong to a user in that project. The first
state read may perform the documented one-time SQLite bootstrap if needed.
"""
import os

import pytest
from fastapi.security import HTTPAuthorizationCredentials

from backend.app.auth import require_user
from backend.app.config import Settings
from backend.app.supabase_store import get_paused


_RUN_LIVE = os.getenv("PERSONAL_AGENT_RUN_LIVE_SUPABASE_TESTS") == "1"
pytestmark = pytest.mark.skipif(not _RUN_LIVE, reason="Opt-in live Supabase test is disabled")


def test_live_user_can_read_own_global_pause_from_supabase():
    url = os.environ["SUPABASE_URL"]
    publishable_key = os.environ["SUPABASE_PUBLISHABLE_KEY"]
    access_token = os.environ["PERSONAL_AGENT_LIVE_SUPABASE_ACCESS_TOKEN"]
    settings = Settings(agent_state_storage="supabase", supabase_url=url, supabase_publishable_key=publishable_key, supabase_jwks_url=os.getenv("SUPABASE_JWKS_URL"))
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=access_token)
    principal = require_user(credentials=credentials, settings=settings)
    assert isinstance(get_paused(principal.user_id, access_token, settings), bool)
