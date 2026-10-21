"""Bounded live infrastructure inspection. No provider calls or setup writes."""
from __future__ import annotations
import hashlib
from pathlib import Path
import subprocess

from scripts.e2e.reporting import ROOT


def local_snapshot():
    from src.config import settings
    from src.services.llm_routing import current_routing
    from src.services.embeddings import specification
    from src.services.semantic_corpus import load_corpus
    from src.services import semantic_v3_corpus as corpus
    if settings.OFFLINE_MODE:
        raise ValueError('suite requires OFFLINE_MODE=false')
    from src.services.llm_routing import validate_runtime_config
    validate_runtime_config()
    routing = current_routing()
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=ROOT, text=True).strip()
    code_hash = hashlib.sha256()
    for tree in ('src', 'scripts'):
        for source in sorted((ROOT / tree).rglob('*.py')):
            code_hash.update(str(source.relative_to(ROOT)).encode() + b'\0' + source.read_bytes() + b'\0')
    files = [Path(settings.LLM_ROUTING_FILE), ROOT / 'examples/e2e/scenarios.json',
             ROOT / 'examples/semantic_v3/taxonomy.json']
    files += sorted((ROOT / 'examples/semantic_evaluation_combined_v1').glob('*.json'))
    data = corpus.load()
    corpus.validate_evaluation_overlap(data)
    return {'commit': git('rev-parse', 'HEAD'), 'dirty': bool(git('status', '--porcelain')),
            'code_hash': code_hash.hexdigest(), 'semantic_mode': settings.SEMANTIC_PIPELINE_MODE,
            'background_enabled': settings.BACKGROUND_WORKFLOWS_ENABLED,
            'assisted_threshold': settings.PROMOTION_THRESHOLD_ASSISTED,
            'live_threshold': settings.PROMOTION_THRESHOLD_LIVE,
            'embedding_spec': specification(), 'routing': routing.model_dump(mode='json'),
            'baseline_corpus_hash': load_corpus().fingerprint, 'v3_corpus_hash': data.fingerprint,
            'v3_eligible': len(corpus.eligible(data)),
            'v3_provisional': any(r.review_status != 'approved' for r in corpus.eligible(data)),
            'collections': [settings.QDRANT_COLLECTION, settings.SEMANTIC_V3_COLLECTION],
            'redis_namespace': settings.REDIS_NAMESPACE, 'queue': settings.WORKFLOW_QUEUE_NAME,
            'file_hashes': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}


def database_snapshot():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import inspect, text
    from src.db.database import engine
    if engine.dialect.name != 'postgresql':
        raise ValueError('live suite refuses SQLite; configure PostgreSQL')
    config = Config(str(ROOT / 'alembic.ini'))
    config.set_main_option('script_location', str(ROOT / 'alembic'))
    expected = sorted(ScriptDirectory.from_config(config).get_heads())
    with engine.connect() as db:
        db.execute(text('SET TRANSACTION READ ONLY'))
        db.execute(text("SET LOCAL statement_timeout = '10000'"))
        current = sorted(MigrationContext.configure(db).get_current_heads())
        from src.db.workflow_schema import validate_workflow_schema
        validate_workflow_schema(db)
        tables = inspect(db).get_table_names()
        required = {'auth_principals', 'auth_credentials', 'auth_events', 'decision_records',
                    'human_review_records', 'escalation_queue', 'request_interpretations',
                    'execution_records', 'simulated_resources', 'consumed_tokens'}
        if not required <= set(tables):
            raise ValueError('required tables missing: ' + ', '.join(sorted(required - set(tables))))
        policies = [dict(r._mapping) for r in db.execute(text(
            'SELECT policy_hash, version, is_active, is_shadow, rules_json FROM policies ORDER BY policy_hash'))]
        constraints = {table: {
            'unique': inspect(db).get_unique_constraints(table),
            'checks': inspect(db).get_check_constraints(table)}
            for table in ('auth_principals', 'workflow_operations', 'decision_records', 'execution_records')
            if table in tables}
    if current != expected:
        raise ValueError('database migration heads differ; apply committed migrations explicitly')
    active = [r for r in policies if r['is_active']]
    if len(active) != 1:
        raise ValueError('expected exactly one active policy; initialize policies explicitly')
    from src.services.policy_loader import canonical_policy_hash
    if any(canonical_policy_hash(r['rules_json']) != r['policy_hash'] for r in policies):
        raise ValueError('stored policy hash mismatch')
    return {'heads': current, 'tables': tables, 'policies': policies, 'constraints': constraints}


def policy_snapshot():
    """Small pointer/artifact read for before/after assertions; no schema introspection."""
    from src.db.database import engine
    from sqlalchemy import text
    if engine.dialect.name != 'postgresql':
        raise ValueError('live suite requires PostgreSQL')
    with engine.connect() as db:
        db.execute(text('SET TRANSACTION READ ONLY'))
        return [dict(row._mapping) for row in db.execute(text(
            'SELECT policy_hash, version, is_active, is_shadow, rules_json FROM policies ORDER BY policy_hash'))]


def corpus_snapshot():
    from qdrant_client import models
    from src.config import settings
    from src.services import semantic_engine, semantic_v3_corpus as v3, vector_setup
    from src.services.semantic_corpus import load_corpus, example_request
    from src.services.embeddings import specification
    baseline = load_corpus()
    corpus = v3.load()
    v3.validate_evaluation_overlap(corpus)
    tasks = [(settings.QDRANT_COLLECTION, {vector_setup.point_id(r.id): {
        'corpus_hash': baseline.fingerprint, 'embedding_spec': specification(),
        'example_id': r.id, 'label': r.label, 'action_family': r.action_family,
        'risk_category': r.risk_category, 'corpus_version': baseline.version,
        'reviewed': baseline.reviewed, 'origin': baseline.origin,
        'text': semantic_engine._request_text(example_request(r.request))} for r in baseline.examples})]
    if settings.SEMANTIC_PIPELINE_MODE != 'baseline':
        eligible = v3.eligible(corpus)
        if not eligible:
            raise ValueError('no eligible v3 retrieval examples')
        tasks.append((settings.SEMANTIC_V3_COLLECTION, {v3.point_id(corpus, r): {
            'corpus_hash': corpus.fingerprint, 'embedding_spec': specification(),
            'text_format': v3.TEXT_FORMAT, 'example_id': r.id,
            **r.model_dump(mode='json'), 'example_id': r.id,
            'action_families': sorted({a.action_family for a in r.actions}),
            'risk_aspects': [item.category.value for item in r.risk_aspects],
            'findings': [item.model_dump(mode='json') for item in r.risk_aspects],
            'text': v3.text(r.instruction), 'origin': corpus.origin,
            'corpus_version': corpus.schema_version} for r in eligible}))
    client = semantic_engine._cloud_client()
    results = []
    try:
        for name, expected in tasks:
            if not client.collection_exists(name):
                results.append({'collection': name, 'complete': False, 'missing': len(expected)})
                continue
            info = client.get_collection(name)
            params = info.config.params.vectors
            if not isinstance(params, models.VectorParams) or params.distance != models.Distance.COSINE:
                raise ValueError('collection requires unnamed cosine vectors: ' + name)
            if specification().startswith('gemini:') and params.size != settings.GEMINI_EMBEDDING_DIMENSIONS:
                raise ValueError('collection embedding dimensions mismatch: ' + name)
            stored = {}
            ids = list(expected)
            for offset in range(0, len(ids), 128):
                stored.update({str(p.id): p.payload or {} for p in client.retrieve(
                    name, ids=ids[offset:offset + 128], with_payload=True, with_vectors=False)})
            wrong = [key for key, payload in expected.items()
                     if key not in stored or any(stored[key].get(k) != v for k, v in payload.items())]
            expected_indexes = ({'action_family', 'label', 'corpus_hash', 'embedding_spec'}
                                if name == settings.QDRANT_COLLECTION else
                                {'action_families', 'risk_aspects', 'label', 'corpus_hash',
                                 'embedding_spec', 'text_format', 'review_status'})
            missing_indexes = sorted(expected_indexes - set(info.payload_schema))
            results.append({'collection': name, 'complete': not wrong and not missing_indexes,
                            'missing_or_mismatched': wrong, 'missing_indexes': missing_indexes,
                            'expected_points': len(expected), 'points_count': info.points_count,
                            'dimensions': params.size, 'payload_indexes': sorted(info.payload_schema)})
    finally:
        client.close()
    return results


def inspect_all(base_url):
    """Failures are separate components; a failed dependency never looks healthy."""
    import httpx
    result = {}
    def check(name, work):
        try:
            result[name] = {'status': 'PASS', 'evidence': work()}
        except Exception as exc:
            # Full exception stays private; the caller redacts it before publication.
            result[name] = {'status': 'BLOCKED', 'error_type': type(exc).__name__, 'detail': str(exc)}
    check('configuration', local_snapshot)
    check('postgresql', database_snapshot)
    def api():
        from scripts.e2e.registry import routes
        with httpx.Client(base_url=base_url, timeout=10) as client:
            health = client.get('/api/v1/health')
            health.raise_for_status()
            schema = client.get('/openapi.json')
            if schema.status_code == 401:
                return {'health': health.json(), 'openapi': 'requires authenticated verification', 'settings_verified': False}
            schema.raise_for_status()
        paths = schema.json()['paths']
        actual = {m.upper() + ' ' + p for p, ops in paths.items() for m in ops
                  if m in {'get', 'post', 'put', 'patch', 'delete'}}
        expected = set(routes()) - {'GET /docs', 'GET /redoc', 'GET /openapi.json'}
        if actual != expected:
            raise ValueError('running API routes differ from local source')
        return {'health': health.json(), 'routes': sorted(actual),
                'openapi_hash': hashlib.sha256(schema.content).hexdigest(),
                'settings_verified': False}
    check('api', api)
    def redis():
        from src.config import settings
        from src.services.embeddings import _redis_client
        if not settings.REDIS_URL:
            raise ValueError('REDIS_URL is missing')
        return {'ping': _redis_client(settings.REDIS_URL).ping(), 'namespace': settings.REDIS_NAMESPACE}
    check('redis', redis)
    check('qdrant', corpus_snapshot)
    def broker():
        from src.config import settings
        from src.workers.celery_app import celery_app
        if not settings.CLOUDAMQP_URL.get_secret_value().startswith('amqps://'):
            raise ValueError('CloudAMQP TLS URL is missing')
        connection = celery_app.connection_for_write(connect_timeout=10)
        try:
            connection.ensure_connection(max_retries=0, timeout=10)
        finally:
            connection.release()
        return {'tls_connected': True, 'queue': settings.WORKFLOW_QUEUE_NAME,
                'queue_declared': False, 'messages_published': 0}
    check('cloudamqp', broker)
    # A TCP connect or queue declaration is not proof of a worker roundtrip.
    result['broker_worker'] = {'status': 'NOT RUN', 'detail': 'verify through background scenario and authenticated diagnostics'}
    return result


def decision(request_id):
    from src.db.database import engine
    from sqlalchemy import text
    with engine.connect() as db:
        db.execute(text('SET TRANSACTION READ ONLY'))
        return [dict(row._mapping) for row in db.execute(text(
            'SELECT * FROM decision_records WHERE request_id = :id'), {'id': request_id})]


def resources(prefix):
    from src.db.database import engine
    from sqlalchemy import text
    with engine.connect() as db:
        db.execute(text('SET TRANSACTION READ ONLY'))
        return {row.resource: row.value_json for row in db.execute(text(
            'SELECT resource, value_json FROM simulated_resources WHERE starts_with(resource, :prefix)'),
            {'prefix': prefix})}
