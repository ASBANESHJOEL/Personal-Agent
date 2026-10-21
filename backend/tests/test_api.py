from contextlib import closing
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from cryptography.hazmat.primitives.asymmetric import rsa

from backend.app import db
from backend.app import auth
from backend.app.auth import Principal, require_user
from backend.app.config import Settings
from backend.app.main import app, health, pause, ready, resume, state
from backend.app.policy import authorize_action


def test_health_and_readiness(client):
    assert health() == {"status": "ok"}
    assert ready()["status"] == "ready"
    paths = set(app.openapi()["paths"])
    assert {"/health", "/ready", "/api/v1/discovery/schedule"}.issubset(paths)


def test_readiness_failure_is_reported(monkeypatch):
    monkeypatch.setattr("backend.app.main.get_settings", lambda: Settings(agent_state_storage="sqlite"))
    monkeypatch.setattr(db, "init_db", lambda: (_ for _ in ()).throw(RuntimeError("storage down")))
    with pytest.raises(HTTPException) as error:
        ready()
    assert error.value.status_code == 503


def test_production_readiness_rejects_local_sqlite(monkeypatch):
    monkeypatch.setattr("backend.app.main.get_settings", lambda: Settings(app_env="production", agent_state_storage="sqlite"))
    with pytest.raises(HTTPException) as error:
        ready()
    assert error.value.status_code == 503
    assert "AGENT_STATE_STORAGE=supabase" in error.value.detail


def test_supabase_readiness_reports_configuration_without_anonymous_state_read(monkeypatch):
    from backend.app import supabase_store
    from backend.app.config import Settings

    missing = supabase_store.readiness(Settings(
        agent_state_storage="supabase", supabase_url=None, supabase_publishable_key=None,
    ))
    assert missing == {
        "status": "not_ready", "storage": "supabase-postgres", "configured": False,
        "verified": False,
        "detail": "SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY are required for Supabase state storage.",
    }

    def unexpected_request(*_args, **_kwargs):
        raise AssertionError("/ready must not make an anonymous Data API request")

    monkeypatch.setattr(supabase_store.httpx, "get", unexpected_request)
    configured_settings = Settings(
        agent_state_storage="supabase",
        supabase_url="https://project.supabase.co",
        supabase_publishable_key="sb_publishable_test",
    )
    monkeypatch.setattr("backend.app.main.get_settings", lambda: configured_settings)
    checked = supabase_store.readiness(Settings(
        agent_state_storage="supabase", supabase_url="https://project.supabase.co",
        supabase_publishable_key="sb_publishable_test",
    ))

    assert checked == {
        "status": "configured",
        "storage": "supabase-postgres",
        "configured": True,
        "verified": False,
        "detail": "Supabase is configured; authenticated storage access has not yet been verified.",
    }
    assert ready() == checked


def test_authentication_required_when_no_token():
    with pytest.raises(HTTPException) as error:
        require_user(credentials=None, settings=Settings())
    assert error.value.status_code == 401


def test_invalid_token_and_missing_verification_configuration_fail_closed():
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="not-a-jwt")
    with pytest.raises(HTTPException) as invalid:
        require_user(credentials=credentials, settings=Settings(supabase_jwt_secret="test-secret-for-local-verification-only-32bytes", supabase_url="https://example.supabase.co"))
    assert invalid.value.status_code == 401
    signed = jwt.encode({"sub": "user", "aud": "authenticated", "exp": datetime.now(timezone.utc) + timedelta(minutes=5)}, "test-secret-for-local-verification-only-32bytes", algorithm="HS256")
    with pytest.raises(HTTPException) as unconfigured:
        require_user(credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=signed), settings=Settings(supabase_url="https://example.supabase.co"))
    assert unconfigured.value.status_code == 503


def test_trusted_jwks_verification_and_cross_user_pause_isolation(monkeypatch, client):
    # Generate a fresh signing key at runtime; no token or private key is persisted.
    generated_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = generated_private.public_key()

    class TrustedJwks:
        def get_signing_key_from_jwt(self, _token):
            return SimpleNamespace(key=public_key, algorithm_name="RS256")

    monkeypatch.setattr(auth, "_jwks_client", lambda _url: TrustedJwks())
    settings = Settings(supabase_url="https://example.supabase.co")
    def principal_for(user_id):
        token = jwt.encode({"sub": user_id, "aud": "authenticated", "iss": "https://example.supabase.co/auth/v1", "exp": datetime.now(timezone.utc) + timedelta(minutes=5)}, generated_private, algorithm="RS256", headers={"kid": "trusted-key"})
        return require_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=token), settings)

    first, second = principal_for("user-one"), principal_for("user-two")
    assert resume(first) == {"paused": False}
    assert state(first) == {"paused": False}
    assert state(second) == {"paused": True}


def test_hs256_token_requires_valid_signature_audience_and_issuer(client):
    secret = "test-secret-for-local-verification-only-32bytes"
    settings = Settings(supabase_url="https://example.supabase.co", supabase_jwt_secret=secret)
    claims = {"sub": "hs-user", "aud": "authenticated", "iss": "https://example.supabase.co/auth/v1", "exp": datetime.now(timezone.utc) + timedelta(minutes=5)}
    signed = jwt.encode(claims, secret, algorithm="HS256")
    principal = require_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=signed), settings)
    assert principal.user_id == "hs-user"
    wrong_issuer = jwt.encode({**claims, "iss": "https://attacker.invalid/auth/v1"}, secret, algorithm="HS256")
    with pytest.raises(HTTPException) as error:
        require_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=wrong_issuer), settings)
    assert error.value.status_code == 401


def test_pause_is_persisted_and_blocks_action(client):
    assert state(Principal("test-user")) == {"paused": True}
    paused = authorize_action(Principal("test-user"), "save_discovery")
    assert paused.reason == "agent_paused"
    assert resume(Principal("test-user")) == {"paused": False}
    decision = authorize_action(Principal("test-user"), "save_discovery")
    assert decision.allowed is True
    pause(Principal("test-user"))
    assert db.get_paused("test-user") is True
    assert authorize_action(Principal("test-user"), "save_discovery").reason == "agent_paused"


def test_public_content_always_requires_explicit_approval(client):
    resume(Principal("test-user"))
    with closing(db.connect()) as connection, connection:
        connection.execute("UPDATE action_policies SET enabled = 1 WHERE user_id = 'test-user' AND action_type = 'public_comment'")
    decision = authorize_action(Principal("test-user"), "public_comment")
    assert decision.allowed is False
    assert decision.reason == "explicit_approval_required"
    db.record_approval("test-user", "public_comment")
    assert authorize_action(Principal("test-user"), "public_comment").allowed is True


def test_unknown_policy_and_github_starring_fail_closed(client):
    resume(Principal("test-user"))
    assert authorize_action(Principal("test-user"), "unknown").reason == "policy_missing"
    assert authorize_action(Principal("test-user"), "github_star").reason == "action_not_supported"


def test_pause_state_is_scoped_per_user_and_uncertainty_blocks(monkeypatch, client):
    db.ensure_user_state("another-user")
    assert authorize_action(Principal("another-user"), "save_discovery").reason == "agent_paused"
    monkeypatch.setattr(db, "get_paused", lambda _user: (_ for _ in ()).throw(RuntimeError("unavailable")))
    assert authorize_action(Principal("test-user"), "save_discovery").reason == "state_unavailable"
