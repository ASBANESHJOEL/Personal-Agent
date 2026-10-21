from dataclasses import dataclass
from dataclasses import field
from functools import lru_cache

import jwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientConnectionError
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import db
from .config import Settings, get_settings

bearer = HTTPBearer(auto_error=False)
ASYMMETRIC_ALGORITHMS = ["ES256", "RS256", "EdDSA"]


@lru_cache(maxsize=4)
def _jwks_client(url: str) -> PyJWKClient:
    return PyJWKClient(url, timeout=3, cache_keys=True)


@dataclass(frozen=True)
class Principal:
    user_id: str
    access_token: str | None = field(default=None, repr=False, compare=False)


def verify_user_token(token: str, settings: Settings) -> Principal:
    if not settings.supabase_url:
        raise HTTPException(status_code=503, detail="SUPABASE_URL is required to verify the token issuer")
    try:
        header = jwt.get_unverified_header(token)
        algorithm = header.get("alg")
        issuer = f"{settings.supabase_url.rstrip('/')}/auth/v1"
        decode_options = {"require": ["sub", "exp", "iss"]}
        if algorithm == "HS256" and settings.supabase_jwt_secret:
            claims = jwt.decode(token, settings.supabase_jwt_secret, algorithms=["HS256"], options=decode_options, audience="authenticated", issuer=issuer)
        elif algorithm in ASYMMETRIC_ALGORITHMS:
            jwks_url = settings.supabase_jwks_url or f"{settings.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
            signing_key = _jwks_client(jwks_url).get_signing_key_from_jwt(token)
            if signing_key.algorithm_name and signing_key.algorithm_name != algorithm:
                raise jwt.InvalidAlgorithmError("Token algorithm does not match its JWKS key")
            claims = jwt.decode(token, signing_key.key, algorithms=ASYMMETRIC_ALGORITHMS, options=decode_options, audience="authenticated", issuer=issuer)
        else:
            raise HTTPException(status_code=503, detail="Configure Supabase HS256 secret or Supabase URL for trusted JWKS verification")
    except HTTPException:
        raise
    except PyJWKClientConnectionError as exc:
        raise HTTPException(status_code=503, detail="Supabase signing keys could not be reached") from exc
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid bearer token") from exc
    return Principal(user_id=claims["sub"], access_token=token)


def require_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer), settings: Settings = Depends(get_settings)) -> Principal:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Bearer token required")
    if settings.production_storage_error:
        raise HTTPException(status_code=503, detail=settings.production_storage_error)
    principal = verify_user_token(credentials.credentials, settings)
    try:
        if settings.agent_state_storage == "sqlite":
            db.ensure_user_state(principal.user_id)
        else:
            from . import supabase_store
            supabase_store.ensure_policy_defaults(principal.user_id, principal.access_token or "", settings)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Agent state unavailable") from exc
    return principal
