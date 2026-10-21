"""Authenticated HTTP, durable request IDs, bounded polling and case evidence."""
from __future__ import annotations
import json
from pathlib import Path
import time
from uuid import uuid4
import httpx
from scripts.e2e.reporting import PRIVATE, redact, write_json


class Blocked(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise AssertionError(message)


class Context:
    def __init__(self, manifest, directory, timeout=1200, storage=None):
        self.storage = storage
        self.manifest, self.directory, self.timeout = manifest, directory, timeout
        path = PRIVATE / manifest['run_id'] / 'credentials.json'
        if path.stat().st_mode & 0o077:
            raise ValueError('credentials must be private (0600)')
        self.credentials = json.loads(path.read_text())['callers']
        self.secrets = [v for row in self.credentials.values() for k, v in row.items()
                        if k in {'api_key', 'password', 'password_hash'}]
        from scripts.e2e.initialize import secret_values
        self.secrets += secret_values()
        self.client = httpx.Client(base_url=manifest['base_url'], timeout=httpx.Timeout(120, connect=10), follow_redirects=False)
        self.case = None
        self.events = []
        self.deadline = None
        self.workers_checked = False
        self.state_path = PRIVATE / manifest['run_id'] / 'state.json'
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}

    def close(self):
        try:
            self.client.close()
        finally:
            if self.storage is not None:
                self.storage.close()

    def begin(self, case):
        self.case, self.events = case, []
        self.deadline = time.monotonic() + self.timeout

    def save(self):
        write_json(self.state_path, self.state, private=True)

    def stable(self, key, factory):
        full = self.case + ':' + key
        if full not in self.state:
            self.state[full] = factory()
            self.save()
        return self.state[full]

    def caller(self, alias):
        if alias not in self.credentials:
            raise Blocked('required fixture missing: ' + alias)
        return self.credentials[alias]

    def headers(self, alias):
        return {} if alias is None else {'X-API-Key': self.caller(alias)['api_key']}

    def path(self, name):
        return self.manifest['resource_prefix'] + self.case.lower() + '-' + name

    def action(self, action='create_file', alias='agent', **updates):
        caller = self.caller(alias)
        value = {'agent_id': caller['agent_id'], 'actor_role': caller['actor_role'],
                 'action_class': action, 'target_resource': self.path('note.txt'),
                 'environment': 'development', 'parameters': (
                     {'content': 'E2E synthetic note'} if action in {'create_file', 'update_file'} else {})}
        value.update(updates)
        return value

    def request(self, method, path, *, alias='agent', payload=None, headers=None, step=None):
        if not path.startswith('/') or path.startswith('//'):
            raise ValueError('HTTP paths must stay on the configured API origin')
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('scenario deadline exhausted; inspect durable state before retrying')
        actual_headers = self.headers(alias) | (headers or {})
        if method != 'GET':
            if not step:
                raise ValueError('mutations require a stable step ID')
            saved = self.stable('request:' + step, lambda: {'method': method, 'path': path,
                'payload': payload, 'key': 'e2e-' + str(uuid4())})
            require((saved['method'], saved['path'], saved['payload']) == (method, path, payload),
                    'resumed mutation payload changed for ' + step)
            actual_headers.setdefault('Idempotency-Key', saved['key'])
        start = time.monotonic()
        try:
            response = self.client.request(method, path, json=payload, headers=actual_headers,
                                           timeout=min(120, remaining))
        except Exception as exc:
            self.events.append({'method': method, 'path': path, 'input': payload,
                                'error_type': type(exc).__name__, 'detail': str(exc)})
            raise
        try:
            body = response.json()
        except ValueError:
            body = response.text
        def collect(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in {'access_token', 'capability_token'} and isinstance(item, str):
                        self.secrets.append(item)
                    collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)
        collect(body)
        recorded_body = body
        if path == '/api/v1/escalations' and isinstance(body, list):
            owned_agents = {r.get('agent_id') for r in self.credentials.values() if r.get('agent_id')}
            recorded_body = [item for item in body if (item.get('request') or {}).get('agent_id') in owned_agents]
        self.events.append({'method': method, 'path': path, 'input': payload,
            'http_status': response.status_code, 'body': recorded_body,
            'seconds': time.monotonic() - start,
            'headers': {k: v for k, v in response.headers.items() if k in {
                'x-correlation-id', 'x-operation-id', 'x-operation-status', 'cache-control', 'retry-after'}}})
        return response

    def resolve(self, response, alias='agent'):
        if response.status_code != 202:
            return response
        accepted = response.json()
        operation = accepted.get('operation_id')
        if not operation:
            return response  # business clarification, not a queued operation
        while time.monotonic() < self.deadline:
            status = self.request('GET', f'/api/v1/operations/{operation}', alias=alias)
            if status.status_code in {429, 502, 503, 504}:
                time.sleep(min(2, max(0, self.deadline - time.monotonic())))
                continue
            require(status.status_code == 200, 'operation status must be readable by owner')
            if status.json()['status'] in {'COMPLETED', 'FAILED'}:
                final = self.request('GET', f'/api/v1/operations/{operation}/result', alias=alias)
                if final.status_code != 202:
                    return final
                # A completed operation can legitimately resolve to a business
                # response with HTTP 202 (for example, needs_clarification).
                # Only keep polling when the result endpoint still identifies
                # an operation; otherwise this is the terminal domain result.
                try:
                    final_body = final.json()
                except ValueError:
                    final_body = None
                if not isinstance(final_body, dict) or not final_body.get('operation_id'):
                    return final
            time.sleep(min(2, max(0, self.deadline - time.monotonic())))
        raise TimeoutError('operation ' + operation + ' exceeded scenario deadline')

    def ensure_workers(self):
        if self.manifest['configuration']['background_enabled'] and not self.workers_checked:
            diagnostics = self.request('GET', '/api/v1/operations/diagnostics', alias='reviewer')
            if diagnostics.status_code != 200:
                raise Blocked('cannot verify worker readiness: diagnostics returned HTTP ' + str(diagnostics.status_code))
            current = diagnostics.json()
            if not current.get('background_workflows_enabled'):
                raise Blocked('running API background setting differs from manifest')
            heartbeats = current.get('process_heartbeats', {})
            if any(heartbeats.get(stage, {}).get('stale', True)
                   for stage in ('dispatcher_heartbeat', 'worker_heartbeat')):
                raise Blocked('dispatcher or worker heartbeat is missing/stale; start the configured processes')
            self.workers_checked = True

    def submit(self, path, payload, *, step, alias='agent'):
        background_route = (path in {'/api/v1/actions/evaluate', '/api/v1/actions/evaluate-prompt'}
                            or path.startswith('/api/v1/actions/clarifications/')
                            or path.startswith('/api/v1/actions/rollback/')
                            or path.startswith('/api/v1/escalations/') and path.endswith('/prepare'))
        if background_route:
            self.ensure_workers()
        # Persist generated defaults too: same request ID/timestamp after restart.
        payload = self.stable('payload:' + step, lambda: payload)
        response = self.request('POST', path, payload=payload, alias=alias, step=step)
        return self.resolve(response, alias)

    def evaluate(self, payload, step='evaluate', alias='agent'):
        return self.submit('/api/v1/actions/evaluate', payload, step=step, alias=alias)

    def evidence(self):
        if self.storage is not None:
            return self.storage.save_evidence(self.case, redact(self.events, self.secrets))
        target = self.directory / 'evidence' / (self.case + '.json')
        write_json(target, redact(self.events, self.secrets))
        return str(target.relative_to(self.directory))
