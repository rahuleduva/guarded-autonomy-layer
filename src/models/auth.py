"""Caller identities, credential digests, and authentication audit records."""
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4
import json

from pydantic import Field as ContractField, model_validator
from sqlalchemy import CheckConstraint, Column, String
from sqlmodel import Field, SQLModel

from src.models.request import ValidatedContract
from src.models.types import JSONType

AGENT_PERMISSIONS = frozenset({'actions:evaluate', 'actions:execute', 'actions:rollback', 'records:read_own'})
HUMAN_PERMISSIONS = frozenset({'evidence:read', 'escalations:prepare', 'escalations:review', 'policies:promote', 'agents:promote'})


class PrincipalWrite(ValidatedContract):
    principal_id: str
    kind: Literal['agent', 'human']
    enabled: bool = True
    agent_id: str | None = None
    actor_role: str | None = None
    username: str | None = ContractField(default=None, max_length=128)
    password_hash: str | None = None
    auth_version: int = ContractField(default=1, ge=1)
    permissions: list[str] = ContractField(default_factory=list)
    created_at: datetime

    @model_validator(mode='after')
    def identity_contract(self):
        if self.kind == 'agent':
            if not self.agent_id or not self.actor_role or self.username is not None or self.password_hash is not None:
                raise ValueError('agent requires agent_id and policy role, and no human login fields')
            allowed = AGENT_PERMISSIONS
        else:
            if not self.username or not self.password_hash or self.agent_id is not None or self.actor_role is not None:
                raise ValueError('human requires username and password hash, and no agent binding')
            if self.username != self.username.strip().casefold():
                raise ValueError('username must be normalized')
            if not self.password_hash.startswith('$argon2id$'):
                raise ValueError('human password must be an Argon2id hash')
            allowed = HUMAN_PERMISSIONS
        if len(set(self.permissions)) != len(self.permissions) or not set(self.permissions) <= allowed:
            raise ValueError('permissions are invalid for principal kind')
        return self


class CredentialWrite(ValidatedContract):
    credential_id: str
    principal_id: str
    key_hash: str = ContractField(pattern=r'^[0-9a-f]{64}$')
    created_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None

    @model_validator(mode='after')
    def expiration_contract(self):
        def utc(value):
            return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
        if self.expires_at is not None and utc(self.expires_at) <= utc(self.created_at):
            raise ValueError('credential expiration must follow creation')
        return self


class AuthPrincipal(SQLModel, table=True):
    __tablename__ = 'auth_principals'
    principal_id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    kind: str
    enabled: bool = True
    agent_id: str | None = Field(default=None, unique=True)
    actor_role: str | None = None
    username: str | None = Field(default=None, unique=True)
    password_hash: str | None = None
    auth_version: int = 1
    permissions: list[str] = Field(default_factory=list, sa_column=Column(JSONType, nullable=False))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    __table_args__ = (
        CheckConstraint("kind IN ('agent', 'human') AND auth_version >= 1 AND length(trim(principal_id)) > 0", name='ck_auth_principal_kind'),
        CheckConstraint("(kind = 'agent' AND agent_id IS NOT NULL AND length(trim(agent_id)) > 0 "
                        "AND actor_role IS NOT NULL AND length(trim(actor_role)) > 0 AND username IS NULL AND password_hash IS NULL) "
                        "OR (kind = 'human' AND agent_id IS NULL AND actor_role IS NULL AND username IS NOT NULL "
                        "AND length(trim(username)) > 0 AND password_hash IS NOT NULL "
                        "AND password_hash LIKE '$argon2id$%')", name='ck_auth_principal_identity'),
        CheckConstraint(
            "jsonb_typeof(permissions) = 'array' AND ((kind = 'agent' AND permissions <@ '"
            + json.dumps(sorted(AGENT_PERMISSIONS)) + "'::jsonb) OR (kind = 'human' AND permissions <@ '"
            + json.dumps(sorted(HUMAN_PERMISSIONS)) + "'::jsonb))", name='ck_auth_principal_permissions'
        ).ddl_if(dialect='postgresql'),
    )


class AuthCredential(SQLModel, table=True):
    __tablename__ = 'auth_credentials'
    credential_id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    principal_id: str = Field(foreign_key='auth_principals.principal_id', index=True)
    key_hash: str = Field(unique=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    __table_args__ = (
        CheckConstraint('length(key_hash) = 64', name='ck_auth_credential_hash'),
        CheckConstraint('expires_at IS NULL OR expires_at > created_at', name='ck_auth_credential_expiration'),
    )


class AuthEventWrite(ValidatedContract):
    event_id: str
    principal_id: str | None = None
    correlation_id: str
    route: str
    outcome: Literal['accepted', 'denied', 'administration']
    reason: str
    created_at: datetime


class AuthEvent(SQLModel, table=True):
    __tablename__ = 'auth_events'
    event_id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    principal_id: str | None = Field(default=None, foreign_key='auth_principals.principal_id', index=True)
    correlation_id: str = Field(index=True)
    route: str
    outcome: str
    reason: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    __table_args__ = (CheckConstraint("outcome IN ('accepted', 'denied', 'administration')", name='ck_auth_event_outcome'),)


class LoginRequest(ValidatedContract):
    username: str = ContractField(min_length=1, max_length=128)
    password: str = ContractField(min_length=1, max_length=1024, repr=False)


class LoginResponse(ValidatedContract):
    access_token: str = ContractField(repr=False)
    token_type: Literal['bearer'] = 'bearer'
    expires_in: int = ContractField(gt=0)


class CallerIdentity(ValidatedContract):
    principal_id: str
    kind: Literal['agent', 'human']
    agent_id: str | None
    actor_role: str | None
    permissions: list[str]
