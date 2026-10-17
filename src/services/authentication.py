"""Verified caller context. All database reads finish before external work."""
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import logging
import re
from threading import BoundedSemaphore
from uuid import uuid4

from fastapi import HTTPException
from jose import JWTError, jwt
from sqlmodel import Session, select

from src.config import settings
from src.models.auth import (AuthCredential, AuthEvent, AuthEventWrite, AuthPrincipal,
                             PrincipalWrite, CredentialWrite, LoginResponse)

logger = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc)


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


@dataclass(frozen=True)
class Caller:
    principal_id: str
    kind: str
    agent_id: str | None
    actor_role: str | None
    permissions: frozenset[str]
    auth_version: int
    credential_id: str | None
    expires_at: datetime | None
    correlation_id: str
    route: str


current: ContextVar[Caller | None] = ContextVar('authenticated_caller', default=None)


def event(session, caller, outcome, reason, *, correlation_id=None, route=None):
    row = AuthEvent(principal_id=caller.principal_id if caller else None,
                    correlation_id=correlation_id or (caller.correlation_id if caller else str(uuid4())),
                    route=route or (caller.route if caller else '<authentication>'),
                    outcome=outcome, reason=reason)
    AuthEventWrite.model_validate(row.model_dump())
    session.add(row)


def audit(engine, caller, outcome, reason, *, correlation_id=None, route=None):
    """Report persistence success so denial handlers can expose audit outages."""
    correlation_id = correlation_id or (caller.correlation_id if caller else str(uuid4()))
    try:
        with Session(engine) as session:
            event(session, caller, outcome, reason, correlation_id=correlation_id, route=route)
            session.commit()
        return True
    except Exception:
        # Never log the exception body: driver errors can include bound values.
        logger.error('authentication audit unavailable correlation_id=%s',
                     correlation_id)
        return False


def validate_access_secret(secret, capability_secret):
    """Reject common setup mistakes; configuration cannot prove randomness."""
    normalized = re.sub(r'[^a-z0-9]', '', secret.casefold())
    placeholder = any(marker in normalized for marker in (
        'changeme', 'placeholder', 'replacewith', 'yourjwtsecret',
        'yourauthjwtsecret', 'youraccesstokensecret', 'yourrandomsecret',
        'devinsecuresecret',
    ))
    if (len(secret.encode()) < 32 or secret != secret.strip()
            or len(set(secret)) < 2 or placeholder or secret == capability_secret):
        raise RuntimeError('AUTH_JWT_SECRET must be a separate randomly generated secret of at least 32 bytes; placeholders are invalid')


def validate_config():
    validate_access_secret(settings.AUTH_JWT_SECRET, settings.SECRET_KEY)
    if not settings.AUTH_JWT_ISSUER.strip() or not settings.AUTH_JWT_AUDIENCE.strip():
        raise RuntimeError('access-token issuer and audience must be configured')
    # Fail startup clearly rather than failing the first human login.
    password_hasher()
    dummy_hash()


@lru_cache(maxsize=1)
def password_hasher():
    from argon2 import PasswordHasher
    from argon2.low_level import Type
    return PasswordHasher(time_cost=3, memory_cost=65536, parallelism=1, type=Type.ID)


@lru_cache(maxsize=1)
def dummy_hash():
    return password_hasher().hash('unused-dummy-password-for-timing')


@lru_cache(maxsize=1)
def password_slots():
    return BoundedSemaphore(settings.AUTH_PASSWORD_MAX_CONCURRENCY)


def _invalid():
    return HTTPException(401, 'invalid or expired credentials', headers={'WWW-Authenticate': 'Bearer'})


def _caller(row, credential_id, expires_at, correlation_id, route):
    PrincipalWrite.model_validate(row.model_dump())
    if not row.enabled:
        raise _invalid()
    return Caller(row.principal_id, row.kind, row.agent_id, row.actor_role,
                  frozenset(row.permissions), row.auth_version, credential_id,
                  expires_at, correlation_id, route)


def authenticate(engine, authorization, api_key, correlation_id, route):
    if authorization is not None and api_key is not None:
        raise HTTPException(400, 'supply exactly one authentication header')
    if api_key is not None:
        if not api_key or len(api_key) > 512:
            raise _invalid()
        digest = hashlib.sha256(api_key.encode()).hexdigest()
        with Session(engine) as session:
            credential = session.exec(select(AuthCredential).where(AuthCredential.key_hash == digest)).one_or_none()
            if credential is None:
                raise _invalid()
            CredentialWrite.model_validate(credential.model_dump())
            if credential.revoked_at is not None or (credential.expires_at and aware(credential.expires_at) <= now()):
                raise _invalid()
            row = session.get(AuthPrincipal, credential.principal_id)
            if row is None:
                raise _invalid()
            return _caller(row, credential.credential_id, credential.expires_at, correlation_id, route)
    if not authorization or len(authorization) > 8192:
        raise _invalid()
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != 'bearer':
        raise _invalid()
    try:
        claims = jwt.decode(parts[1], settings.AUTH_JWT_SECRET, algorithms=['HS256'],
                            audience=settings.AUTH_JWT_AUDIENCE, issuer=settings.AUTH_JWT_ISSUER,
                            options={'require_exp': True, 'require_iat': True, 'require_sub': True,
                                     'require_aud': True, 'require_iss': True})
        if (claims.get('token_type') != 'human_access' or type(claims.get('auth_version')) is not int
                or type(claims.get('exp')) is not int or type(claims.get('iat')) is not int
                or claims['iat'] > int(now().timestamp()) or claims['exp'] <= claims['iat']):
            raise _invalid()
        expires_at = datetime.fromtimestamp(claims['exp'], timezone.utc)
    except (JWTError, ValueError, TypeError, OverflowError):
        raise _invalid() from None
    with Session(engine) as session:
        row = session.get(AuthPrincipal, claims['sub'])
        if row is None or row.kind != 'human' or row.auth_version != claims['auth_version']:
            raise _invalid()
        return _caller(row, None, expires_at, correlation_id, route)


ROUTE_PERMISSIONS = {
    'evaluate': ('agent', {'actions:evaluate'}),
    'interpret': ('agent', {'actions:evaluate'}),
    'clarify': ('agent', {'actions:evaluate'}),
    'execute': ('agent', {'actions:execute'}),
    'rollback': ('agent', {'actions:rollback'}),
    'review': ('human', {'evidence:read', 'escalations:review'}),
    'prepare_review': ('human', {'evidence:read', 'escalations:prepare'}),
    'promote_policy': ('human', {'policies:promote'}),
    'promote_agent': ('human', {'agents:promote'}),
    'evidence': ('human', {'evidence:read'}),
    'read_own': ('agent', {'records:read_own'}),
}


def require(caller, kind):
    expected, permissions = ROUTE_PERMISSIONS[kind]
    if caller.kind != expected or not permissions <= caller.permissions:
        raise HTTPException(403, 'caller lacks required permission')


def refresh(session, caller):
    """Lock principal then credential; administrators use the same lock order."""
    row = session.get(AuthPrincipal, caller.principal_id, with_for_update=True, populate_existing=True)
    if row is None or not row.enabled or row.auth_version != caller.auth_version:
        raise _invalid()
    PrincipalWrite.model_validate(row.model_dump())
    if caller.expires_at is not None and aware(caller.expires_at) <= now():
        raise _invalid()
    if caller.credential_id:
        key = session.get(AuthCredential, caller.credential_id, with_for_update=True, populate_existing=True)
        if (key is None or key.principal_id != row.principal_id or key.revoked_at is not None
                or (key.expires_at and aware(key.expires_at) <= now())):
            raise _invalid()
    return _caller(row, caller.credential_id, caller.expires_at, caller.correlation_id, caller.route)


def own_decision(session, caller, decision_id):
    from src.models.decision import DecisionRecord
    row = session.get(DecisionRecord, decision_id)
    if row is None or (row.agent_id, row.actor_role) != (caller.agent_id, caller.actor_role):
        raise HTTPException(404, 'decision not found')


def authorize_operation(session, caller, kind, scope, payload):
    caller = refresh(session, caller)
    require(caller, kind)
    if kind in {'evaluate', 'interpret', 'clarify'}:
        if (payload.get('agent_id'), payload.get('actor_role')) != (caller.agent_id, caller.actor_role):
            raise HTTPException(403, 'submitted identity differs from authenticated caller')
        if kind == 'clarify':
            from src.models.clarification import RequestInterpretation
            row = session.get(RequestInterpretation, scope)
            if row is None or (row.agent_id, row.actor_role) != (caller.agent_id, caller.actor_role):
                raise HTTPException(404, 'clarification request not found')
    elif kind in {'review', 'promote_policy', 'promote_agent'}:
        if payload.get('reviewer_id') != caller.principal_id:
            raise HTTPException(403, 'submitted reviewer differs from authenticated caller')
    elif kind == 'execute':
        own_decision(session, caller, scope)
    elif kind == 'rollback':
        from src.models.runtime import ExecutionRecord
        row = session.get(ExecutionRecord, scope)
        if row is None:
            raise HTTPException(404, 'execution not found')
        own_decision(session, caller, row.decision_id)
    return caller


def authorize_read(session, caller, category, object_id=None):
    caller = refresh(session, caller)
    if category == 'operation':
        from src.models.workflow import WorkflowOperation
        row = session.get(WorkflowOperation, object_id)
        if row is None:
            raise HTTPException(404, 'operation not found')
        if row.caller_principal_id == caller.principal_id:
            require(caller, row.kind)
        else:
            if caller.kind != 'human':
                raise HTTPException(404, 'operation not found')
            require(caller, 'evidence')
        return row
    if category == 'clarification':
        from src.models.clarification import RequestInterpretation
        require(caller, 'read_own')
        row = session.get(RequestInterpretation, object_id)
        if row is None or (row.agent_id, row.actor_role) != (caller.agent_id, caller.actor_role):
            raise HTTPException(404, 'clarification request not found')
        return row
    require(caller, 'evidence')


@lru_cache(maxsize=1)
def limiter_client():
    import redis
    return redis.Redis.from_url(settings.REDIS_URL, socket_timeout=2, socket_connect_timeout=2)


def throttle(username, address):
    if not settings.REDIS_URL:
        raise HTTPException(503, 'login temporarily unavailable')
    script = """local a=redis.call('INCR',KEYS[1]); if a==1 then redis.call('EXPIRE',KEYS[1],60) end;
local b=redis.call('INCR',KEYS[2]); if b==1 then redis.call('EXPIRE',KEYS[2],60) end;
return {a,b,redis.call('TTL',KEYS[1]),redis.call('TTL',KEYS[2])}"""
    prefix = settings.REDIS_NAMESPACE + ':auth:login:'
    try:
        counts = limiter_client().eval(script, 2, prefix + 'user:' + hashlib.sha256(username.encode()).hexdigest(),
                                       prefix + 'address:' + hashlib.sha256(address.encode()).hexdigest())
    except Exception:
        raise HTTPException(503, 'login temporarily unavailable') from None
    if counts[0] > settings.AUTH_LOGIN_USERNAME_LIMIT or counts[1] > settings.AUTH_LOGIN_ADDRESS_LIMIT:
        raise HTTPException(429, 'too many login attempts', headers={'Retry-After': str(max(1, counts[2], counts[3]))})


def login(engine, request, address, correlation_id):
    username = request.username.strip().casefold()
    throttle(username, address)
    with Session(engine) as session:
        row = session.exec(select(AuthPrincipal).where(AuthPrincipal.username == username)).one_or_none()
        snapshot = row.model_dump() if row else None
    # Password hashing is intentionally outside the transaction.
    from argon2.exceptions import VerificationError, InvalidHashError
    slots = password_slots()
    if not slots.acquire(timeout=1):
        raise HTTPException(503, 'login temporarily unavailable')
    try:
        try:
            matched = password_hasher().verify(snapshot['password_hash'] if snapshot else dummy_hash(), request.password)
        except (VerificationError, InvalidHashError):
            matched = False
    finally:
        slots.release()
    if not matched or not snapshot or snapshot['kind'] != 'human':
        raise _invalid()
    with Session(engine) as session:
        row = session.get(AuthPrincipal, snapshot['principal_id'], with_for_update=True)
        if (not row.enabled or row.password_hash != snapshot['password_hash']
                or row.auth_version != snapshot['auth_version']):
            raise _invalid()
        caller = _caller(row, None, None, correlation_id, '/api/v1/auth/login')
        issued = int(now().timestamp())
        access = jwt.encode({'sub': row.principal_id, 'iat': issued,
            'exp': issued + settings.AUTH_ACCESS_TOKEN_TTL_SECONDS, 'iss': settings.AUTH_JWT_ISSUER,
            'aud': settings.AUTH_JWT_AUDIENCE, 'token_type': 'human_access', 'auth_version': row.auth_version},
            settings.AUTH_JWT_SECRET, algorithm='HS256')
        event(session, caller, 'accepted', 'login')
        session.commit()
    return LoginResponse(access_token=access, expires_in=settings.AUTH_ACCESS_TOKEN_TTL_SECONDS)
