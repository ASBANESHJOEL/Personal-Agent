from dataclasses import dataclass

from . import db
from .auth import Principal
from .config import get_settings
from . import supabase_store

PUBLIC_ACTIONS = {"publish_post", "public_comment"}
FORBIDDEN_ACTIONS = {"github_star"}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    requires_approval: bool = True


def authorize_action(principal: Principal | None, action_type: str) -> Decision:
    """Pure fail-closed gateway. It deliberately performs no action execution."""
    if principal is None:
        return Decision(False, "authenticated_user_required")
    if action_type in FORBIDDEN_ACTIONS:
        return Decision(False, "action_not_supported")
    settings = get_settings()
    if settings.production_storage_error:
        return Decision(False, "state_unavailable")
    try:
        if settings.agent_state_storage == "supabase":
            if not principal.access_token:
                return Decision(False, "state_unavailable")
            paused = supabase_store.get_paused(principal.user_id, principal.access_token)
            policy = supabase_store.get_policy(principal.user_id, action_type, principal.access_token, settings)
        else:
            paused = db.get_paused(principal.user_id)
            policy = db.get_policy(principal.user_id, action_type)
    except Exception:
        return Decision(False, "state_unavailable")
    if paused:
        return Decision(False, "agent_paused")
    if policy is None:
        return Decision(False, "policy_missing")
    enabled, requires_approval = policy
    if not enabled:
        return Decision(False, "policy_disabled", requires_approval)
    if action_type in PUBLIC_ACTIONS:
        requires_approval = True
    if requires_approval:
        try:
            if settings.agent_state_storage == "supabase":
                approved = supabase_store.has_approval(principal.user_id, action_type, principal.access_token or "", settings)
            else:
                approved = db.has_approval(principal.user_id, action_type)
        except Exception:
            return Decision(False, "approval_state_unavailable", True)
    else:
        approved = False
    if requires_approval and not approved:
        return Decision(False, "explicit_approval_required", True)
    return Decision(True, "authorized_for_review", requires_approval)
