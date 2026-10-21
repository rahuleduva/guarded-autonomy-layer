"""Inspect, provision run-owned callers, and verify live prerequisites."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from scripts.e2e import evidence, registry
from scripts.e2e.fixtures import provision, run_lock
from scripts.e2e.reporting import PRIVATE, SETUP, now, redact, run_name, write_json


def base_url(value):
    parsed = urlsplit(value)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {'', '/'}:
        raise argparse.ArgumentTypeError('base URL must be http(s), without credentials/query/fragment')
    return value.rstrip('/')


def secret_values():
    from src.config import settings
    values = []
    for key in type(settings).model_fields:
        value = getattr(settings, key)
        if hasattr(value, 'get_secret_value'):
            value = value.get_secret_value()
        if (isinstance(value, str) and len(value) >= 8
                and any(word in key for word in ('API_KEY', 'JWT_SECRET', 'SECRET_KEY', 'DATABASE_URL', 'REDIS_URL', 'CLOUDAMQP_URL', 'MONGODB_CONNECTION_STRING'))):
            values.append(value)
    return values



def verify_callers(manifest):
    import httpx
    private = json.loads((PRIVATE / manifest['run_id'] / 'credentials.json').read_text())
    with httpx.Client(base_url=manifest['base_url'], timeout=30) as client:
        for alias, row in private['callers'].items():
            response = client.get('/api/v1/auth/me', headers={'X-API-Key': row['api_key']})
            response.raise_for_status()
            if response.json() != {k: row.get(k) for k in (
                    'principal_id', 'kind', 'agent_id', 'actor_role', 'permissions')}:
                raise ValueError('running server identity mismatch: ' + alias)
        schema = client.get('/openapi.json', headers={'X-API-Key': private['callers']['reviewer']['api_key']})
        schema.raise_for_status()
        actual = {m.upper() + ' ' + p for p, ops in schema.json()['paths'].items()
                  for m in ops if m in {'get', 'post', 'put', 'patch', 'delete'}}
        expected = set(registry.routes()) - {'GET /docs', 'GET /redoc', 'GET /openapi.json'}
        if actual != expected:
            raise ValueError('running API route inventory differs from local source')
    return {'identities_verified': True, 'routes_verified': True,
            'process_settings_verified': False,
            'limitation': 'API does not expose configuration fingerprints; local config is not proof of worker/API settings'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True, type=run_name)
    parser.add_argument('--base-url', type=base_url, default='http://127.0.0.1:8000')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan', action='store_true')
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--verify', action='store_true')
    parser.add_argument('--seed-corpus', action='store_true', help='explicitly repair missing configured corpus points during apply')
    args = parser.parse_args()
    if args.seed_corpus and not args.apply:
        parser.error('--seed-corpus requires --apply')
    with run_lock(args.run_id):
        output = SETUP / args.run_id
        path = output / 'manifest.json'
        old = json.loads(path.read_text()) if path.exists() else None
        if old and old['base_url'] != args.base_url:
            parser.error('existing run is bound to another API URL; use a fresh run ID')
        snapshot = evidence.inspect_all(args.base_url)
        secrets = secret_values()
        write_json(output / 'preflight.json', redact(snapshot, secrets))
        blocked = [k for k, v in snapshot.items() if v['status'] == 'BLOCKED']
        configuration = snapshot.get('configuration', {}).get('evidence')
        db = snapshot.get('postgresql', {}).get('evidence')
        if old and configuration and old.get('configuration') != configuration:
            # Never silently rewrite the baseline of a previously frozen run.
            raise SystemExit('configuration/code/data changed since manifest creation; use a fresh run ID')
        if old and db and old.get('database', {}).get('policies') != db['policies']:
            raise SystemExit('policy snapshot changed; use a fresh run ID')
        if args.verify:
            if not old:
                parser.error('no manifest; run --apply first')
            result = verify_callers(old)
            write_json(output / 'verification.json', result)
            if blocked or any(not r['complete'] for r in snapshot['qdrant']['evidence']):
                raise SystemExit('verification blocked; see preflight.json')
            print('Verified callers, API routes, PostgreSQL schema, Redis and corpus. Worker readiness requires BG scenarios.')
            return
        if args.plan:
            write_json(output / 'setup-plan.json', redact({
                'run_id': args.run_id, 'created_at': now(), 'blocked_components': blocked,
                'actions': ['create/reuse dedicated run principals and private credentials',
                            'preserve policy pointers and all historical data',
                            'repair missing corpus only with --apply --seed-corpus'],
                'snapshot': snapshot}, secrets))
            print('Setup plan: ' + str(output / 'setup-plan.json'))
            return
        if blocked:
            raise SystemExit('setup blocked: ' + ', '.join(blocked) + '; see preflight.json')
        if any(not r['complete'] for r in snapshot['qdrant']['evidence']):
            if not args.seed_corpus:
                raise SystemExit('corpus incomplete; review preflight, then explicitly use --apply --seed-corpus')
            from src.services import vector_setup, semantic_v3_corpus
            from src.config import settings
            for row in snapshot['qdrant']['evidence']:
                if not row['complete']:
                    if row['collection'] == settings.QDRANT_COLLECTION:
                        vector_setup.seed()
                    else:
                        semantic_v3_corpus.seed()
            verified = evidence.corpus_snapshot()
            if any(not row['complete'] for row in verified):
                raise SystemExit('corpus verification failed after seeding')
        policy = next(r for r in db['policies'] if r['is_active'])
        callers = provision(args.run_id, policy)
        manifest = old or {'schema_version': 1, 'run_id': args.run_id, 'created_at': now(),
            'base_url': args.base_url, 'registry_hash': registry.fingerprint(),
            'route_inventory': registry.inventory(), 'configuration': configuration,
            'database': db, 'policy': policy, 'callers': callers,
            'resource_prefix': policy['rules_json']['sandbox_root'].rstrip('/') + '/e2e-' + args.run_id + '/',
            'private_credentials': '.e2e/<run_id>/credentials.json'}
        write_json(path, manifest)
        write_json(output / 'verification.json', verify_callers(manifest))
        print('Manifest: ' + str(path))


if __name__ == '__main__':
    main()
