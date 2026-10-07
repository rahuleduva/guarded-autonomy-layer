"""Issue and verify short-lived, action-scoped capability JWTs (C.5, C.6)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from jose import JWTError, jwt

from src.config import settings
from src.models.request import ActionRequest

_ALGORITHM = "HS256"
_REQUIRED_STRING_CLAIMS = (
    "jti",
    "decision_id",
    "policy_hash",
    "agent_id",
    "actor_role",
    "action_class",
    "target_resource",
)


def issue(
    decision_id: str,
    policy_hash: str,
    request: ActionRequest,
    ttl_seconds: int,
) -> str:
    """Create a signed token authorizing exactly this request and decision."""
    if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) or ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be a positive integer")
    if not isinstance(decision_id, str) or not decision_id:
        raise ValueError("decision_id must be a non-empty string")
    if not isinstance(policy_hash, str) or not policy_hash:
        raise ValueError("policy_hash must be a non-empty string")
    if settings.JWT_ALGORITHM != _ALGORITHM:
        raise ValueError(f"only {_ALGORITHM} is supported for capability tokens")
    if not settings.SECRET_KEY:
        raise ValueError("SECRET_KEY must be configured to issue capability tokens")

    issued_at = int(datetime.now(timezone.utc).timestamp())
    claims = {
        "jti": str(uuid4()),
        "decision_id": decision_id,
        "policy_hash": policy_hash,
        "agent_id": request.agent_id,
        "actor_role": request.actor_role,
        "action_class": request.action_class,
        "target_resource": request.target_resource,
        "iat": issued_at,
        "exp": issued_at + ttl_seconds,
    }
    return jwt.encode(claims, settings.SECRET_KEY, algorithm=_ALGORITHM)


def verify(token: str) -> Optional[dict[str, Any]]:
    """Verify signature, expiry, and required claim shape; return claims or None.

    Single-use enforcement belongs to the executor, which checks and records
    ``jti`` in the consumed-token table in the same transaction as execution.
    """
    if not isinstance(token, str) or not token:
        return None
    if settings.JWT_ALGORITHM != _ALGORITHM or not settings.SECRET_KEY:
        return None
    try:
        claims = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[_ALGORITHM],
            options={"require_exp": True, "require_iat": True, "require_jti": True},
        )
    except (JWTError, TypeError, ValueError, OverflowError):
        return None

    if not isinstance(claims, dict):
        return None
    if any(not isinstance(claims.get(name), str) or not claims[name] for name in _REQUIRED_STRING_CLAIMS):
        return None
    if not isinstance(claims.get("iat"), int) or isinstance(claims["iat"], bool):
        return None
    if not isinstance(claims.get("exp"), int) or isinstance(claims["exp"], bool):
        return None
    return claims
