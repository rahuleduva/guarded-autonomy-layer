"""Provision one local E2E agent and reviewer in the configured PostgreSQL DB.

Generated API keys and the reviewer password are written to the ignored .env
with owner-only file permissions. The script never prints credential values.
It is safe to rerun: it reuses still-valid credentials and rotates only missing
or invalid E2E credentials for the dedicated E2E principals.
"""
from __future__ import annotations

from datetime import timedelta
import hashlib
import json
import os
import secrets
import tempfile
from uuid import uuid4

from dotenv import dotenv_values
from sqlmodel import Session, select

from src.config import ENV_FILE
from src.db.database import engine
from src.models.auth import (HUMAN_PERMISSIONS, AuthCredential, AuthPrincipal,
                             CredentialWrite, PrincipalWrite)
from src.models.autonomy import AutonomyState
from src.models.clarification import RequestInterpretation
from src.models.decision import DecisionRecord
from src.services import authentication as auth
from src.services.policy_store import active

ENV_KEYS = (
    'E2E_MAIN_AGENT', 'E2E_AGENT_API_KEY', 'E2E_REVIEWER',
    'E2E_REVIEWER_USERNAME', 'E2E_REVIEWER_API_KEY', 'E2E_REVIEWER_PASSWORD',
)
AGENT_ROLE = 'workspace_agent'
E2E_AGENT_PERMISSIONS = frozenset({'actions:evaluate', 'records:read_own'})


def _env_values() -> dict[str, str]:
    return {key: value for key, value in dotenv_values(ENV_FILE).items()
            if value is not None}


def _write_env(updates: dict[str, str]) -> None:
    original = ENV_FILE.read_text(encoding='utf-8') if ENV_FILE.exists() else ''
    lines = original.splitlines()
    replaced: set[str] = set()
    output: list[str] = []
    for line in lines:
        name, separator, _value = line.partition('=')
        if separator and name.strip() in updates:
            key = name.strip()
            if key not in replaced:
                output.append(f'{key}={json.dumps(updates[key])}')
                replaced.add(key)
            continue
        output.append(line)
    for key, value in updates.items():
        if key not in replaced:
            output.append(f'{key}={json.dumps(value)}')
    content = '\n'.join(output).rstrip() + '\n'
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix='.env.e2e.', dir=ENV_FILE.parent, text=True)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, ENV_FILE)
        os.chmod(ENV_FILE, 0o600)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _matches_password(encoded: str, raw: str) -> bool:
    from argon2.exceptions import InvalidHashError, VerificationError
    try:
        return auth.password_hasher().verify(encoded, raw)
    except (InvalidHashError, VerificationError):
        return False


def _active_key(session: Session, principal: AuthPrincipal, raw_key: str) -> bool:
    digest = hashlib.sha256(raw_key.encode()).hexdigest()
    row = session.exec(select(AuthCredential).where(
        AuthCredential.principal_id == principal.principal_id,
        AuthCredential.key_hash == digest,
    )).one_or_none()
    return bool(row and row.revoked_at is None
                and (row.expires_at is None or auth.aware(row.expires_at) > auth.now()))


def _issue_key(session: Session, principal: AuthPrincipal) -> str:
    for row in session.exec(select(AuthCredential).where(
            AuthCredential.principal_id == principal.principal_id,
            AuthCredential.revoked_at.is_(None))):
        row.revoked_at = auth.now()
        session.add(row)
    raw = 'gal_' + secrets.token_urlsafe(32)
    credential = AuthCredential(
        principal_id=principal.principal_id,
        key_hash=hashlib.sha256(raw.encode()).hexdigest(),
        expires_at=auth.now() + timedelta(days=90),
    )
    CredentialWrite.model_validate(credential.model_dump())
    session.add(credential)
    session.flush()
    return raw


def main() -> None:
    if engine.dialect.name != 'postgresql':
        raise SystemExit('E2E credential seeding requires the configured PostgreSQL database.')
    auth.validate_config()
    existing = _env_values()
    found = [key for key in ENV_KEYS if existing.get(key)]
    if found and len(found) != len(ENV_KEYS):
        raise SystemExit('E2E credential settings are incomplete in .env; resolve them before seeding.')

    if found:
        agent_id = existing['E2E_MAIN_AGENT']
        reviewer_id = existing['E2E_REVIEWER']
        username = existing['E2E_REVIEWER_USERNAME'].strip().casefold()
        agent_key = existing['E2E_AGENT_API_KEY']
        reviewer_key = existing['E2E_REVIEWER_API_KEY']
        reviewer_password = existing['E2E_REVIEWER_PASSWORD']
    else:
        suffix = uuid4().hex[:12]
        agent_id = 'e2e-agent-' + suffix
        reviewer_id = 'e2e-reviewer-' + suffix
        username = reviewer_id
        agent_key = reviewer_key = reviewer_password = ''

    # Password verification and hashing are intentionally outside transactions.
    with Session(engine) as session:
        reviewer_snapshot = session.get(AuthPrincipal, reviewer_id)
        if reviewer_snapshot is None:
            reviewer_snapshot = session.exec(select(AuthPrincipal).where(
                AuthPrincipal.username == username)).one_or_none()
        reviewer_password_hash = (reviewer_snapshot.password_hash if reviewer_snapshot else None)
    if reviewer_snapshot is None:
        reviewer_password = reviewer_password or secrets.token_urlsafe(32)
        replacement_password_hash = auth.password_hasher().hash(reviewer_password)
    elif not _matches_password(reviewer_password_hash, reviewer_password):
        reviewer_password = secrets.token_urlsafe(32)
        replacement_password_hash = auth.password_hasher().hash(reviewer_password)
    else:
        replacement_password_hash = None

    new_agent_key = new_reviewer_key = False
    with Session(engine) as session:
        policy = active(session)
        if AGENT_ROLE not in policy.rules_json.get('role_permissions', {}):
            raise SystemExit('The active policy has no workspace_agent role; E2E seeding stopped.')

        agent = session.exec(select(AuthPrincipal).where(AuthPrincipal.agent_id == agent_id)
                             .with_for_update()).one_or_none()
        if agent is None:
            historical = any(session.exec(select(model).where(model.agent_id == agent_id)).first()
                             for model in (DecisionRecord, RequestInterpretation, AutonomyState))
            if historical:
                raise SystemExit('The E2E agent has historical records but no auth principal; refusing implicit adoption.')
            agent = AuthPrincipal(principal_id='e2e-agent-' + uuid4().hex,
                                  kind='agent', agent_id=agent_id,
                                  actor_role=AGENT_ROLE,
                                  permissions=sorted(E2E_AGENT_PERMISSIONS))
            PrincipalWrite.model_validate(agent.model_dump())
            session.add(agent)
            session.flush()
        if agent.kind != 'agent' or agent.actor_role != AGENT_ROLE:
            raise SystemExit('The configured E2E agent principal has an unexpected binding.')
        if not agent.enabled:
            agent.enabled = True
            session.add(agent)
        if set(agent.permissions) != E2E_AGENT_PERMISSIONS:
            agent.permissions = sorted(E2E_AGENT_PERMISSIONS)
            session.add(agent)
        if not agent_key or not _active_key(session, agent, agent_key):
            agent_key = _issue_key(session, agent)
            new_agent_key = True

        reviewer = session.get(AuthPrincipal, reviewer_id, with_for_update=True)
        if reviewer is None:
            reviewer = session.exec(select(AuthPrincipal).where(
                AuthPrincipal.username == username).with_for_update()).one_or_none()
        if reviewer is None:
            reviewer = AuthPrincipal(principal_id=reviewer_id, kind='human',
                username=username, password_hash=replacement_password_hash,
                permissions=sorted(HUMAN_PERMISSIONS))
            PrincipalWrite.model_validate(reviewer.model_dump())
            session.add(reviewer)
            session.flush()
        if (reviewer.kind != 'human' or reviewer.username != username
                or reviewer.principal_id != reviewer_id):
            raise SystemExit('The configured E2E reviewer principal has an unexpected binding.')
        if reviewer_snapshot is not None and reviewer.password_hash != reviewer_password_hash:
            raise SystemExit('The E2E reviewer password changed concurrently; rerun the seeder.')
        if not reviewer.enabled:
            reviewer.enabled = True
            reviewer.auth_version += 1
            session.add(reviewer)
        if set(reviewer.permissions) != HUMAN_PERMISSIONS:
            reviewer.permissions = sorted(HUMAN_PERMISSIONS)
            session.add(reviewer)
        if replacement_password_hash is not None and reviewer_snapshot is not None:
            reviewer.password_hash = replacement_password_hash
            reviewer.auth_version += 1
            session.add(reviewer)

        PrincipalWrite.model_validate(agent.model_dump())
        PrincipalWrite.model_validate(reviewer.model_dump())
        if not reviewer_key or not _active_key(session, reviewer, reviewer_key):
            reviewer_key = _issue_key(session, reviewer)
            new_reviewer_key = True

        auth.event(session, None, 'administration', 'seed_e2e_agent',
                   route='local:seed_e2e_auth')
        auth.event(session, None, 'administration', 'seed_e2e_reviewer',
                   route='local:seed_e2e_auth')
        session.commit()

    _write_env({
        'E2E_MAIN_AGENT': agent_id,
        'E2E_AGENT_API_KEY': agent_key,
        'E2E_REVIEWER': reviewer_id,
        'E2E_REVIEWER_USERNAME': username,
        'E2E_REVIEWER_API_KEY': reviewer_key,
        'E2E_REVIEWER_PASSWORD': reviewer_password,
    })
    print('E2E agent and reviewer are ready; credentials saved to the ignored .env with mode 600.')
    print('Agent API key rotated:', new_agent_key)
    print('Reviewer API key rotated:', new_reviewer_key)


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        # SQL driver errors can contain bound data; never print exception text.
        raise SystemExit('E2E credential seeding failed (' + type(exc).__name__ + ').') from None
