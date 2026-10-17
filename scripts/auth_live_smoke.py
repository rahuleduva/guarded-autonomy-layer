"""Live auth boundary checks; no resource executions or provider calls.

Set E2E_AGENT_API_KEY, E2E_REVIEWER_API_KEY, E2E_REVIEWER_USERNAME.
Optional E2E_BASE (default http://127.0.0.1:8000). Password is prompted.
Requires an agent with actions:evaluate and records:read_own, and a human with
evidence:read, escalations:review, and agents:promote. Optional E2E_MAIN_AGENT
and E2E_REVIEWER assert the expected agent binding and human principal ID.
Requires applied auth migration and provisioned callers.
"""
from getpass import getpass
import os
from uuid import uuid4
import httpx
from dotenv import load_dotenv
from src.config import ENV_FILE


def main():
    load_dotenv(ENV_FILE, override=False)
    agent_key = os.environ['E2E_AGENT_API_KEY']
    human_key = os.environ['E2E_REVIEWER_API_KEY']
    username = os.environ['E2E_REVIEWER_USERNAME']
    password = os.environ.get('E2E_REVIEWER_PASSWORD') or getpass('Reviewer password: ')
    with httpx.Client(base_url=os.environ.get('E2E_BASE', 'http://127.0.0.1:8000'), timeout=30) as client:
        def check(label, method, path, status, **kwargs):
            response = client.request(method, '/api/v1' + path, **kwargs)
            if response.status_code != status:
                raise AssertionError(f'{label}: expected {status}, got {response.status_code}')
            print(label + ': PASS')
            return response.json()
        check('public health', 'GET', '/health', 200)
        check('missing credentials', 'GET', '/auth/me', 401)
        check('invalid key', 'GET', '/auth/me', 401, headers={'X-API-Key': 'invalid'})
        agent = check('agent key', 'GET', '/auth/me', 200, headers={'X-API-Key': agent_key})
        human = check('human key', 'GET', '/auth/me', 200, headers={'X-API-Key': human_key})
        if (agent.get('kind') != 'agent' or not agent.get('agent_id') or not agent.get('actor_role')
                or not {'actions:evaluate', 'records:read_own'} <= set(agent.get('permissions', []))):
            raise AssertionError('agent test key must belong to an agent with evaluation and own-record permissions')
        if (human.get('kind') != 'human'
                or not {'evidence:read', 'escalations:review', 'agents:promote'} <= set(human.get('permissions', []))):
            raise AssertionError('reviewer test key must belong to a human with evidence, review, and agent-promotion permissions')
        if os.environ.get('E2E_MAIN_AGENT') and agent['agent_id'] != os.environ['E2E_MAIN_AGENT']:
            raise AssertionError('agent key does not match E2E_MAIN_AGENT')
        if os.environ.get('E2E_REVIEWER') and human['principal_id'] != os.environ['E2E_REVIEWER']:
            raise AssertionError('human key does not match E2E_REVIEWER')
        print('Caller kinds, permissions, and configured identity bindings: PASS')
        login = check('human password login', 'POST', '/auth/login', 200,
                      json={'username': username, 'password': password})
        bearer = {'Authorization': 'Bearer ' + login['access_token']}
        jwt_identity = check('human JWT', 'GET', '/auth/me', 200, headers=bearer)
        if human != jwt_identity:
            raise AssertionError('human key and JWT must resolve to the same identity')
        check('human key can read escalation evidence', 'GET', '/escalations', 200,
              headers={'X-API-Key': human_key})
        check('human JWT can read escalation evidence', 'GET', '/escalations', 200, headers=bearer)
        check('conflicting headers', 'GET', '/auth/me', 400, headers=dict(bearer, **{'X-API-Key': agent_key}))
        check('API key is not a bearer JWT', 'GET', '/auth/me', 401,
              headers={'Authorization': 'Bearer ' + agent_key})
        check('agent cannot view escalation evidence', 'GET', '/escalations', 403,
              headers={'X-API-Key': agent_key})
        check('agent cannot submit review', 'POST', '/control/approve/' + str(uuid4()), 403,
              headers={'X-API-Key': agent_key}, json={})
        check('agent cannot promote', 'POST', '/control/promote-agent', 403,
              headers={'X-API-Key': agent_key}, json={})
        check('human cannot submit agent request', 'POST', '/actions/evaluate', 403, headers=bearer, json={})
        check('identity spoofing blocked before providers', 'POST', '/actions/evaluate', 403,
              headers={'X-API-Key': agent_key}, json={
                  'agent_id': 'different-' + str(uuid4()), 'actor_role': agent['actor_role'],
                  'action_class': 'read_file', 'target_resource': '/not-executed.txt'})
        private_marker = 'validation-must-not-echo-' + str(uuid4())
        invalid_login = check('invalid login validation hides input', 'POST', '/auth/login', 422,
                              json={'password': private_marker})
        if private_marker in str(invalid_login):
            raise AssertionError('login validation response exposed supplied password')
        print('Authentication boundaries passed. No resource actions or provider calls were made.')


if __name__ == '__main__':
    main()
