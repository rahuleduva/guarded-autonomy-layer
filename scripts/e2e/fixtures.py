"""Run-owned identities only. Never reset shared passwords or grant autonomy."""
from __future__ import annotations
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import secrets
from datetime import timedelta

from scripts.e2e.reporting import PRIVATE, run_name, write_json


@contextmanager
def run_lock(run_id):
    directory = PRIVATE / run_name(run_id)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    # Serialize local initialization AND scenario runs against this manifest.
    with (directory / 'lock').open('a') as stream:
        os.chmod(directory / 'lock', 0o600)
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield directory
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def provision(run_id, policy):
    from sqlmodel import Session, select
    from src.db.database import engine
    from src.models.auth import (AGENT_PERMISSIONS, HUMAN_PERMISSIONS, AuthPrincipal,
                                 AuthCredential, PrincipalWrite, CredentialWrite)
    from src.services import authentication as auth
    if engine.dialect.name != 'postgresql':
        raise ValueError('fixture creation requires PostgreSQL')
    run_name(run_id)
    suffix = hashlib.sha256(run_id.encode()).hexdigest()[:16]
    roles = policy['rules_json']['role_permissions']
    contracts = {
        'agent': ('agent', 'workspace_agent', AGENT_PERMISSIONS),
        'other_agent': ('agent', 'workspace_agent', AGENT_PERMISSIONS),
        'reader': ('agent', 'read_only_agent', {'actions:evaluate', 'records:read_own'}),
        'admin': ('agent', 'admin_agent', AGENT_PERMISSIONS),
        'executor_agent': ('agent', 'admin_agent', AGENT_PERMISSIONS),
        'reviewer': ('human', None, HUMAN_PERMISSIONS),
        'reviewer_two': ('human', None, HUMAN_PERMISSIONS),
        'limited_human': ('human', None, {'evidence:read'}),
    }
    path = PRIVATE / run_id / 'credentials.json'
    if path.exists():
        if path.stat().st_mode & 0o077:
            raise ValueError('private credential file must have mode 0600')
        credentials = json.loads(path.read_text())
    else:
        credentials = {'run_id': run_id, 'callers': {}}
    if credentials['run_id'] != run_id:
        raise ValueError('private credential run mismatch')
    for alias, (kind, role, permissions) in contracts.items():
        if kind == 'agent' and role not in roles:
            continue
        principal_id = f'e2e-{suffix}-{alias}'
        if alias not in credentials['callers']:
            row = {'principal_id': principal_id, 'kind': kind,
                   'permissions': sorted(permissions), 'api_key': 'gal_' + secrets.token_urlsafe(32)}
            if kind == 'agent':
                row.update(agent_id=principal_id, actor_role=role)
            else:
                password = secrets.token_urlsafe(32)
                row.update(username=principal_id, password=password,
                           password_hash=auth.password_hasher().hash(password))
            credentials['callers'][alias] = row
    # Save generated material BEFORE committing DB writes. Interrupted setup
    # resumes with identical keys; we never leave committed, lost credentials.
    write_json(path, credentials, private=True)
    with Session(engine) as db:
        from sqlalchemy import text
        lock = int.from_bytes(hashlib.sha256(('e2e:' + run_id).encode()).digest()[:8], 'big', signed=True)
        db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': lock})
        for alias, row in credentials['callers'].items():
            existing = db.get(AuthPrincipal, row['principal_id'])
            values = {k: v for k, v in row.items() if k not in {'api_key', 'password'}}
            if existing is None:
                existing = AuthPrincipal(**values)
                PrincipalWrite.model_validate(existing.model_dump())
                db.add(existing)
                db.flush()
            else:
                for field, value in values.items():
                    if getattr(existing, field) != value:
                        raise ValueError('fixture principal changed; refusing to reset it: ' + alias)
                if not existing.enabled:
                    raise ValueError('fixture disabled; explicit credential recovery required')
            digest = hashlib.sha256(row['api_key'].encode()).hexdigest()
            key = db.exec(select(AuthCredential).where(AuthCredential.key_hash == digest)).one_or_none()
            if key is None:
                key = AuthCredential(principal_id=existing.principal_id, key_hash=digest,
                                     expires_at=auth.now() + timedelta(days=30))
                CredentialWrite.model_validate(key.model_dump())
                db.add(key)
            elif (key.principal_id != existing.principal_id or key.revoked_at is not None
                  or key.expires_at is not None and auth.aware(key.expires_at) <= auth.now()):
                raise ValueError('fixture credential expired/revoked; explicit recovery required')
        auth.event(db, None, 'administration', 'initialize_e2e:' + run_id, route='local:e2e.initialize')
        db.commit()
    return {alias: {k: v for k, v in row.items() if k not in {'api_key', 'password', 'password_hash'}}
            for alias, row in credentials['callers'].items()}
