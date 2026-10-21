"""Live PostgreSQL/Celery acceptance checks for background workflow delivery.

Requires an upgraded PostgreSQL schema, BACKGROUND_WORKFLOWS_ENABLED=true,
the API, dispatcher and worker running, plus E2E_MAIN_AGENT/E2E_AGENT_API_KEY.
The checks never execute an action and write each report to a new file.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import time
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from sqlalchemy import text
from sqlmodel import Session

from scripts.e2e.api_polling import request_and_wait
from src.config import settings
from src.db.database import engine


CASES = ('operation_roundtrip', 'duplicate_submission')


def credentials(parser):
    agent = os.environ.get('E2E_MAIN_AGENT')
    key = os.environ.get('E2E_AGENT_API_KEY')
    if not agent or not key:
        parser.error('set E2E_MAIN_AGENT and E2E_AGENT_API_KEY')
    return agent, key


def evaluate(base_url, key, payload, idempotency_key=None):
    headers = {'X-API-Key': key}
    if idempotency_key:
        headers['Idempotency-Key'] = idempotency_key
    with httpx.Client(base_url=base_url, timeout=120) as client:
        return request_and_wait(client, 'POST', '/api/v1/actions/evaluate',
                                json=payload, headers=headers, timeout_seconds=1200)


def run_roundtrip(base_url, agent, key, run_id):
    payload = {'request_id': str(uuid4()), 'agent_id': agent, 'actor_role': 'workspace_agent',
        'action_class': 'create_file', 'target_resource': f'/srv/agent/workspace/bg-{run_id}.txt',
        'environment': 'development', 'parameters': {'content': 'background workflow smoke'}}
    response, operation_id, accepted = evaluate(base_url, key, payload)
    body = response.json()
    assert response.status_code == 200, {'status': response.status_code, 'body': body}
    assert body.get('decision_id') and body.get('action_request', {}).get('request_id') == payload['request_id']
    assert operation_id is not None and accepted is not None and accepted.status_code == 202
    assert response.request.url.path.endswith('/result')
    assert response.headers.get('X-Operation-Status') == 'COMPLETED'
    assert response.headers.get('Cache-Control') == 'no-store'
    return {'operation_id': operation_id, 'decision_id': body['decision_id'],
            'request_id': payload['request_id'], 'accepted_status': accepted.status_code,
            'result_status': response.status_code}


def run_duplicate(base_url, agent, key, run_id):
    request_id, idem = str(uuid4()), 'background-' + run_id
    payload = {'request_id': request_id, 'agent_id': agent, 'actor_role': 'workspace_agent',
        'action_class': 'create_file', 'target_resource': f'/srv/agent/workspace/bg-dup-{run_id}.txt',
        'environment': 'development', 'parameters': {'content': 'idempotency smoke'}}
    def submit():
        headers = {'X-API-Key': key, 'Idempotency-Key': idem}
        with httpx.Client(base_url=base_url, timeout=120) as client:
            return client.post('/api/v1/actions/evaluate', json=payload, headers=headers)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = [future.result() for future in [pool.submit(submit), pool.submit(submit)]]
    parsed = []
    for response in (first, second):
        assert response.status_code in {200, 202}, (response.status_code, response.text)
        body = response.json()
        op_id = response.headers.get('X-Operation-ID') or body.get('operation_id')
        assert op_id, body
        parsed.append((response, op_id, body))
    assert parsed[0][1] == parsed[1][1], [item[1] for item in parsed]
    operation_id = parsed[0][1]
    with httpx.Client(base_url=base_url, timeout=120, headers={'X-API-Key': key}) as client:
        deadline = time.monotonic() + 1200
        while time.monotonic() < deadline:
            status = client.get(f'/api/v1/operations/{operation_id}')
            assert status.status_code == 200, status.text
            state = status.json()['status']
            if state in {'COMPLETED', 'FAILED'}:
                break
            time.sleep(2)
        else:
            raise TimeoutError(f'operation {operation_id} did not finish')
        result = client.get(f'/api/v1/operations/{operation_id}/result')
    assert result.status_code == 200, (result.status_code, result.text)
    body = result.json()
    assert body.get('action_request', {}).get('request_id') == request_id
    with engine.connect() as connection:
        assert connection.dialect.name == 'postgresql', 'live E2E requires PostgreSQL'
        count = connection.execute(text(
            'SELECT count(*) FROM decision_records WHERE request_id = :request_id'),
            {'request_id': request_id}).scalar_one()
    assert count == 1, f'expected one decision for duplicate submissions, found {count}'
    return {'operation_id': operation_id, 'decision_id': body['decision_id'],
            'request_id': request_id, 'submission_statuses': [first.status_code, second.status_code],
            'decision_rows': count}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--case', choices=CASES, action='append')
    parser.add_argument('--list-cases', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.list_cases:
        print('\n'.join(CASES))
        return
    if not settings.BACKGROUND_WORKFLOWS_ENABLED:
        parser.error('BACKGROUND_WORKFLOWS_ENABLED must be true for live acceptance checks')
    with engine.connect() as connection:
        if connection.dialect.name != 'postgresql':
            parser.error('live E2E script refuses SQLite and requires PostgreSQL')
    agent, key = credentials(parser)
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '_' + uuid4().hex[:8]
    cases = args.case or list(CASES)
    functions = {'operation_roundtrip': run_roundtrip, 'duplicate_submission': run_duplicate}
    report = {'run_id': run_id, 'created_at': datetime.now(timezone.utc).isoformat(),
              'execution_endpoint_called': False, 'cases': []}
    for case in cases:
        item = {'case': case, 'passed': False}
        report['cases'].append(item)
        try:
            item['evidence'] = functions[case](args.base_url.rstrip('/'), agent, key, run_id)
            item['passed'] = True
        except Exception as exc:
            item['error_type'] = type(exc).__name__
    report['passed'] = all(item['passed'] for item in report['cases'])
    output = args.output or Path('reports/e2e/narrow') / f'background_live_e2e_{run_id}.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'report': str(output), 'cases': report['cases']}, indent=2))
    raise SystemExit(0 if report['passed'] else 1)


if __name__ == '__main__':
    main()
