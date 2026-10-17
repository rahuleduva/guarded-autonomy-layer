"""Local credential administration: python -m src.auth_cli --help.

Passwords are prompted without echo. Issued API keys are displayed once.
"""
import argparse
from datetime import timedelta
from getpass import getpass
import hashlib
import secrets
from uuid import uuid4

from sqlmodel import Session, select

from src.db.database import engine
from src.models.auth import AuthCredential, AuthPrincipal, CredentialWrite, PrincipalWrite
from src.services import authentication as auth


def password():
    first = getpass('Password (12–1024 characters): ')
    second = getpass('Confirm password: ')
    if first != second or not 12 <= len(first) <= 1024 or not first.strip():
        raise ValueError('passwords must match and contain 12–1024 characters')
    # Perform expensive password hashing before opening a transaction.
    return auth.password_hasher().hash(first)


def save_principal(session, row):
    PrincipalWrite.model_validate(row.model_dump())
    session.add(row)
    session.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for kind in ('agent', 'human'):
        create = commands.add_parser('create-' + kind)
        create.add_argument('--principal-id', default=None)
        create.add_argument('--permission', action='append', default=[])
        if kind == 'agent':
            create.add_argument('--agent-id', required=True)
            create.add_argument('--actor-role', required=True)
            create.add_argument('--adopt-existing', action='store_true',
                                help='Explicitly authorize binding to historical agent records')
        else:
            create.add_argument('--username', required=True)
    issue = commands.add_parser('issue-key')
    issue.add_argument('--principal-id', required=True)
    issue.add_argument('--expires-days', type=int, default=90)
    revoke = commands.add_parser('revoke-key')
    revoke.add_argument('--credential-id', required=True)
    for name in ('disable', 'enable', 'reset-password', 'revoke-sessions', 'set-permissions'):
        command = commands.add_parser(name)
        command.add_argument('--principal-id', required=True)
        if name == 'set-permissions':
            command.add_argument('--permission', action='append', default=[])
    args = parser.parse_args()
    if engine.dialect.name != 'postgresql':
        parser.error('credential administration requires the configured PostgreSQL database')
    hashed = password() if args.command in {'create-human', 'reset-password'} else None
    raw_key = None
    with Session(engine) as session:
        if args.command.startswith('create-'):
            kind = args.command.removeprefix('create-')
            row = AuthPrincipal(principal_id=args.principal_id or str(uuid4()), kind=kind,
                                permissions=args.permission)
            if kind == 'agent':
                from src.models.decision import DecisionRecord
                from src.models.clarification import RequestInterpretation
                from src.models.autonomy import AutonomyState
                from src.services.policy_store import active
                policy = active(session)
                if args.actor_role not in policy.rules_json.get('role_permissions', {}):
                    raise ValueError('actor role is absent from the active policy')
                exists = any(session.exec(select(model).where(model.agent_id == args.agent_id)).first() is not None
                             for model in (DecisionRecord, RequestInterpretation, AutonomyState))
                if exists and not args.adopt_existing:
                    raise ValueError('historical agent data exists; explicit --adopt-existing is required')
                row.agent_id, row.actor_role = args.agent_id, args.actor_role
            else:
                row.username, row.password_hash = args.username.strip().casefold(), hashed
            save_principal(session, row)
        elif args.command == 'revoke-key':
            # Read binding first without taking the credential lock. Always lock
            # principal before credential, matching protected workflow mutations.
            key = session.get(AuthCredential, args.credential_id)
            if key is None:
                raise ValueError('credential not found')
            principal_id = key.principal_id
            row = session.get(AuthPrincipal, principal_id, with_for_update=True, populate_existing=True)
            key = session.get(AuthCredential, args.credential_id, with_for_update=True, populate_existing=True)
            key.revoked_at = auth.now()
            session.add(key)
        else:
            row = session.get(AuthPrincipal, args.principal_id, with_for_update=True)
            if row is None:
                raise ValueError('principal not found')
            if args.command == 'issue-key':
                if not row.enabled or not 1 <= args.expires_days <= 3650:
                    raise ValueError('enabled principal and expiration of 1–3650 days required')
                raw_key = 'gal_' + secrets.token_urlsafe(32)
                key = AuthCredential(principal_id=row.principal_id,
                    key_hash=hashlib.sha256(raw_key.encode()).hexdigest(),
                    expires_at=auth.now() + timedelta(days=args.expires_days))
                CredentialWrite.model_validate(key.model_dump())
                session.add(key)
            elif args.command in {'enable', 'disable'}:
                row.enabled = args.command == 'enable'
                row.auth_version += 1
            elif args.command == 'reset-password':
                if row.kind != 'human':
                    raise ValueError('only humans have passwords')
                row.password_hash, row.auth_version = hashed, row.auth_version + 1
            elif args.command == 'revoke-sessions':
                row.auth_version += 1
            elif args.command == 'set-permissions':
                row.permissions = args.permission
            save_principal(session, row)
        auth.event(session, None, 'administration', args.command + ':' + row.principal_id,
                   route='local:auth_cli')
        session.commit()
        print('Principal ID:', row.principal_id)
        if args.command in {'issue-key', 'revoke-key'}:
            print('Credential ID:', key.credential_id)
        if raw_key:
            print('API key (shown once):', raw_key)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # SQL errors can contain password hashes or key digests; do not dump them.
        raise SystemExit('Credential operation failed (' + type(exc).__name__ + '). Check inputs and applied schema.') from None
