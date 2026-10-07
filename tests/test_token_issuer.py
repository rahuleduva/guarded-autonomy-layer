"""Capability token claim binding and verification behavior."""

from datetime import datetime, timedelta, timezone

from jose import jwt
import pytest

from src.config import settings
from src.models.request import ActionRequest
from src.services.token_issuer import issue, verify


def request() -> ActionRequest:
    return ActionRequest(
        agent_id="agent-1",
        actor_role="workspace_agent",
        action_class="update_file",
        target_resource="/srv/agent/workspace/a.txt",
    )


def test_issue_binds_action_and_decision_claims(monkeypatch):
    monkeypatch.setattr(settings, "SECRET_KEY", "test-signing-secret")
    action = request()
    claims = verify(issue("decision-1", "policy-hash", action, 60))

    assert claims is not None
    assert claims["decision_id"] == "decision-1"
    assert claims["policy_hash"] == "policy-hash"
    assert claims["agent_id"] == action.agent_id
    assert claims["actor_role"] == action.actor_role
    assert claims["action_class"] == action.action_class
    assert claims["target_resource"] == action.target_resource
    assert claims["exp"] - claims["iat"] == 60
    assert len(claims["jti"]) == 36


def test_verify_rejects_tampered_signature(monkeypatch):
    monkeypatch.setattr(settings, "SECRET_KEY", "test-signing-secret")
    token = issue("decision-1", "policy-hash", request(), 60)
    header, payload, signature = token.split(".")
    replacement = "A" if signature[0] != "A" else "B"

    assert verify(f"{header}.{payload}.{replacement}{signature[1:]}") is None


def test_verify_rejects_expired_token(monkeypatch):
    secret = "test-signing-secret"
    monkeypatch.setattr(settings, "SECRET_KEY", secret)
    now = datetime.now(timezone.utc)
    claims = {
        "jti": "nonce",
        "decision_id": "decision-1",
        "policy_hash": "policy-hash",
        "agent_id": "agent-1",
        "actor_role": "workspace_agent",
        "action_class": "update_file",
        "target_resource": "/srv/agent/workspace/a.txt",
        "iat": int((now - timedelta(minutes=2)).timestamp()),
        "exp": int((now - timedelta(minutes=1)).timestamp()),
    }
    token = jwt.encode(claims, secret, algorithm="HS256")

    assert verify(token) is None


def test_issue_rejects_nonpositive_ttl(monkeypatch):
    monkeypatch.setattr(settings, "SECRET_KEY", "test-signing-secret")

    with pytest.raises(ValueError, match="positive integer"):
        issue("decision-1", "policy-hash", request(), 0)
