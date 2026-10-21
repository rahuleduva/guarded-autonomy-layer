"""Atomic suite evidence; secrets remain in the private run directory."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import tempfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = ROOT / '.e2e'
SETUP = ROOT / 'reports/e2e/setup'


def run_name(value: str) -> str:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', value):
        raise ValueError('run ID must contain 1–80 letters, digits, underscores or hyphens')
    return value


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value, *, private=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700 if private else 0o755)
    if private:
        os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w') as stream:
            if private:
                os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, indent=2, sort_keys=True, default=str, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def redact(value, secrets=()):
    """Redact keys recursively AND actual secret values inside prose/tracebacks."""
    if isinstance(value, dict):
        sensitive = {'password', 'password_hash', 'api_key', 'x-api-key', 'authorization',
                     'cookie', 'set-cookie', 'secret', 'database_url', 'redis_url',
                     'cloudamqp_url', 'mongodb_connection_string', 'access_token', 'capability_token', 'token',
                     'auth_jwt_secret', 'secret_key', 'capability_token_hash'}
        return {k: '[REDACTED]' if k.casefold() in sensitive
                else redact(v, secrets) for k, v in value.items()}

    if isinstance(value, (tuple, list)):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in sorted(set(s for s in secrets if s), key=len, reverse=True):
            value = value.replace(secret, '[REDACTED]')
        value = re.sub(r'\b(?:postgres(?:ql)?|rediss?|amqps?|mongodb(?:\+srv)?)://[^\s\"\']+', '[REDACTED_URL]', value)
        value = re.sub(r'\bgal_[A-Za-z0-9_-]+', '[REDACTED_KEY]', value)
        value = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[REDACTED_JWT]', value)
    return value


def save_report(directory, report, secrets=(), storage=None):
    clean = redact(report, secrets)
    if storage is not None:
        storage.save_report(clean)
        return
    write_json(directory / 'results.json', clean)
    counts = {status: sum(r['status'] == status for r in clean['cases'])
              for status in ('PASS', 'FAIL', 'BLOCKED', 'SKIPPED', 'NOT RUN')}
    lines = ['# Live E2E suite', '', f"Run: `{clean['run_id']}`", '',
             ' | '.join(f'{key}: {value}' for key, value in counts.items()), '',
             'PASS describes executed assertions only. Unselected cases remain NOT RUN.', '',
             '| Case | Status | Detail |', '| --- | --- | --- |']
    for row in clean['cases']:
        detail = str(row.get('reason', '')).replace('|', '\\|').replace('\n', ' ')
        lines.append(f"| {row['id']} | {row['status']} | {detail} |")
    (directory / 'REPORT.md').write_text('\n'.join(lines) + '\n')
