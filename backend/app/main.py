from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Path, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import db
from .auth import Principal, require_user
from .config import get_settings
from .policy import authorize_action
from . import supabase_store
from . import discovery
from . import schedule_api


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    if settings.production_storage_error:
        raise RuntimeError(settings.production_storage_error)
    if settings.agent_state_storage == "sqlite":
        db.init_db()
    yield


app = FastAPI(title="Personal Agent API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=get_settings().cors_origin_list, allow_credentials=True, allow_methods=["GET", "POST", "DELETE", "PATCH"], allow_headers=["Authorization", "Content-Type"])
app.include_router(schedule_api.router)


class ActionRequest(BaseModel):
    action_type: str = Field(min_length=1, max_length=64)
    content: str | None = Field(default=None, max_length=10_000)


class DiscoveryStatusRequest(BaseModel):
    status: str = Field(pattern="^(saved|dismissed)$")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ready")
def ready():
    settings = get_settings()
    if settings.production_storage_error:
        raise HTTPException(status_code=503, detail=settings.production_storage_error)
    if settings.agent_state_storage == "supabase":
        return supabase_store.readiness(settings)
    try:
        db.init_db()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Database is not ready") from exc
    return {"status": "ready", "storage": "local-sqlite", "configured": True, "verified": True}


@app.get("/api/v1/state")
def state(principal: Principal = Depends(require_user)):
    try:
        if get_settings().agent_state_storage == "supabase":
            if not principal.access_token:
                raise HTTPException(status_code=401, detail="Verified user token is required for cloud state")
            return {"paused": supabase_store.get_paused(principal.user_id, principal.access_token)}
        return {"paused": db.get_paused(principal.user_id)}
    except supabase_store.CloudStoreError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Agent state unavailable") from exc


@app.post("/api/v1/state/pause")
def pause(principal: Principal = Depends(require_user)):
    try:
        if get_settings().agent_state_storage == "supabase":
            if not principal.access_token:
                raise HTTPException(status_code=401, detail="Verified user token is required for cloud state")
            return {"paused": supabase_store.set_paused(principal.user_id, principal.access_token, True)}
        db.set_paused(principal.user_id, True)
        return {"paused": True}
    except supabase_store.CloudStoreError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Could not persist pause state") from exc


@app.delete("/api/v1/state/pause")
def resume(principal: Principal = Depends(require_user)):
    try:
        if get_settings().agent_state_storage == "supabase":
            if not principal.access_token:
                raise HTTPException(status_code=401, detail="Verified user token is required for cloud state")
            return {"paused": supabase_store.set_paused(principal.user_id, principal.access_token, False)}
        db.set_paused(principal.user_id, False)
        return {"paused": False}
    except supabase_store.CloudStoreError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Could not persist pause state") from exc


@app.post("/api/v1/actions/authorize")
def check_action(request: ActionRequest, principal: Principal = Depends(require_user)):
    decision = authorize_action(principal, request.action_type)
    if not decision.allowed:
        raise HTTPException(status_code=403, detail={"reason": decision.reason, "requires_approval": decision.requires_approval})
    # Milestone 1 only evaluates authorization. No connector or execution path exists.
    return {"authorized": True, "reason": decision.reason, "requires_approval": decision.requires_approval, "executed": False}


@app.post("/api/v1/discovery/run", response_model=discovery.DiscoveryResponse)
def run_discovery(request: discovery.DiscoveryRunRequest, principal: Principal = Depends(require_user)):
    try:
        return discovery.run_discovery(principal, request)
    except supabase_store.CloudStoreError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Discovery results could not be stored safely.") from exc


@app.get("/api/v1/discovery", response_model=list[discovery.DiscoveryItem])
def get_discovery(status: str | None = Query(default=None, pattern="^(proposed|saved|dismissed)$"), principal: Principal = Depends(require_user)):
    try:
        return discovery.list_items(principal, status)
    except supabase_store.CloudStoreError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.patch("/api/v1/discovery/{item_id}", response_model=discovery.DiscoveryItem)
def update_discovery(item_id: str = Path(..., min_length=32, max_length=64, pattern="^(?:[0-9a-f]{32}|[0-9a-f-]{36}|[0-9a-f]{64})$"), request: DiscoveryStatusRequest = ..., principal: Principal = Depends(require_user)):
    try:
        return discovery.set_item_status(principal, item_id, request.status)  # principal comes only from verified bearer token
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Discovery item was not found for this user.") from exc
    except supabase_store.CloudStoreError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
