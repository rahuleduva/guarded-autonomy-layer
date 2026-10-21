"""Current public API scenario implementations. Expectations live in the registry."""
from __future__ import annotations
import copy
from uuid import uuid4
from scripts.e2e.common import Blocked, require
from scripts.e2e import evidence


def status(response, expected):
    require(response.status_code == expected,
            f'expected HTTP {expected}, received {response.status_code}: {response.text[:3000]}')
    return response.json() if response.headers.get('content-type', '').startswith('application/json') else response.text


def http_case(ctx, case):
    config = case['input']
    alias = config.get('alias')
    payload = config.get('json')
    headers = dict(config.get('headers', {}))
    if config.get('spoof_agent'):
        payload = ctx.action(agent_id='not-the-authenticated-agent')
    if config.get('login'):
        caller = ctx.caller('reviewer')
        payload = {'username': caller['username'], 'password': caller['password']}
    response = ctx.request(config.get('method', 'GET'), config['path'], alias=alias,
                           payload=payload, headers=headers, step='request')
    body = status(response, case['expected']['http'])
    for field, expected in case['expected'].get('fields', {}).items():
        require(body[field] == expected, 'unexpected ' + field)
    if config.get('login'):
        require(body['token_type'] == 'bearer' and body['expires_in'] > 0, 'login contract')
        identity = status(ctx.request('GET', '/api/v1/auth/me', alias=None,
                         headers={'Authorization': 'Bearer ' + body['access_token']}), 200)
        require(identity['principal_id'] == ctx.caller('reviewer')['principal_id'], 'JWT identity differs from key identity')
        require(response.headers.get('cache-control') == 'no-store', 'login must disable caching')
    elif config['path'] == '/api/v1/auth/me' and case['expected']['http'] == 200:
        require(body['principal_id'] == ctx.caller(alias)['principal_id'], 'identity binding mismatch')
    return {'http_status': response.status_code}


def policy_case(ctx, case):
    config = case['input']
    alias = config.get('alias', 'agent')
    payload = ctx.action(alias=alias)
    payload.update(copy.deepcopy(config.get('updates', {})))
    if config.get('escape') == 'sibling':
        payload['target_resource'] = ctx.manifest['policy']['rules_json']['sandbox_root'] + '-other/file.txt'
    elif config.get('escape') == 'destination':
        payload.update(action_class='move_file', parameters={'destination': '/outside/destination.txt'})
    response = ctx.evaluate(payload, alias=alias)
    body = status(response, 200)
    expected = case['expected']['decision']
    require(body['decision'] == expected, f'expected {expected}, got {body["decision"]}')
    require(not body.get('capability_token'), 'denied/held action cannot carry a capability')
    rows = evidence.decision(body['request_id'])
    require(len(rows) == 1 and rows[0]['outcome'] == expected, 'decision ledger differs from response')
    if case['id'] == 'POLICY-004':
        refusal = body.get('unsupported_action')
        recorded = rows[0]['evidence_json'].get('unsupported_action')
        require(body['decision'] == 'DENIED' and refusal is not None,
                'unsupported action must return a structured denial')
        require(refusal.get('code') == 'UNSUPPORTED_ACTION'
                and refusal.get('action_class') == payload['action_class']
                and refusal.get('reason') == body['reason'],
                'unsupported-action response code, action, and reason must agree')
        require(recorded is not None
                and recorded.get('action_class') == payload['action_class']
                and recorded.get('reason') == refusal['reason']
                and 'semantic_pipeline' not in rows[0]['evidence_json'],
                'decision ledger must preserve the same refusal without claiming model analysis')
    return {'decision_id': body['decision_id'], 'request_id': body['request_id'], 'ledger_rows': len(rows)}


def parse_case(ctx, case):
    config = case['input']
    alias = config.get('alias', 'admin')
    caller = ctx.caller(alias)
    target, destination = ctx.path('cache-note.txt.bak'), ctx.path('renamed.txt.bak')
    prompt = config['prompt'].replace('{target}', target).replace('{destination}', destination)
    response = ctx.submit('/api/v1/actions/evaluate-prompt', {'agent_id': caller['agent_id'],
        'actor_role': caller['actor_role'], 'raw_prompt': prompt}, step='parse', alias=alias)
    body = status(response, case['expected']['http'])
    if 'action_class' in case['expected']:
        parsed = body['action_request']
        require(parsed['action_class'] == case['expected']['action_class'], 'parser changed action')
        require(parsed['target_resource'] == target, 'parser changed path, including .bak suffix')
        require(parsed['environment'] == 'development', 'parser lost explicit environment')
        if case['expected'].get('destination'):
            require(parsed['parameters'].get('destination') == destination, 'parser lost destination')
        if 'content' in case['expected']:
            require(parsed['parameters'].get('content') == case['expected']['content'], 'parser changed quoted content')
    elif case['expected'].get('status') == 'rejected':
        require(body['status'] == 'rejected' and body['decision_stage'] == 'PARSER'
                and body['decision'] == 'DENIED' and body['validation_errors'], 'explicit parser denial required')
        rows = evidence.decision(body['request_id'])
        require(len(rows) == 1 and rows[0]['decision_stage'] == 'PARSER'
                and rows[0]['action_class'] is None and not rows[0]['capability_token_hash'],
                'parser rejection ledger contract failed')
    elif case['expected'].get('status') == 'needs_clarification':
        require(body['status'] == 'needs_clarification' and body['validation_errors'] and body['questions'],
                'clarification must carry questions and errors')
        ctx.state['clarification'] = {'body': body, 'alias': alias, 'target': target}
        ctx.save()
    if body.get('decision_stage') == 'PARSER':
        ctx.state['PARSER_REJECTION'] = body
        ctx.save()
    return {'request_id': body['request_id'], 'decision_id': body.get('decision_id'),
            'interpretation_id': body.get('interpretation_id')}


def clarify_case(ctx, case):
    saved = ctx.state.get('clarification')
    if not saved:
        raise Blocked('PARSE-009 must create a clarification first')
    body, alias = saved['body'], saved['alias']
    identity = ctx.caller(alias)
    path = '/api/v1/actions/clarifications/' + body['interpretation_id']
    if case['input'].get('wrong_owner'):
        response = ctx.request('GET', path + '?agent_id=' + ctx.caller('other_agent')['agent_id']
            + '&actor_role=workspace_agent', alias='other_agent')
        status(response, 404)
        return {'interpretation_id': body['interpretation_id']}
    read = ctx.request('GET', path + '?agent_id=' + identity['agent_id'] + '&actor_role=' + identity['actor_role'], alias=alias)
    status(read, 200)
    answer = {'agent_id': identity['agent_id'], 'actor_role': identity['actor_role'],
        'revision': body['revision'], 'clarified_prompt': 'Move ' + saved['target']
            + ' to ' + ctx.path('clarified-destination.txt.bak') + ' in the development environment.'}
    result = status(ctx.submit(path, answer, step='answer', alias=alias), 200)
    require(result['action_request']['target_resource'] == saved['target'], 'clarified path mismatch')
    require(result['action_request']['action_class'] == 'move_file'
            and result['action_request']['parameters'].get('destination') == ctx.path('clarified-destination.txt.bak'),
            'clarification must complete the original move operation')
    duplicate = status(ctx.submit(path, answer, step='answer', alias=alias), 200)
    require(result['decision_id'] == duplicate['decision_id'], 'identical clarification created another decision')
    return {'decision_id': result['decision_id']}


def semantic_payload(ctx, case):
    """Build only the operation's arguments; expected risk labels never enter inputs."""
    config = case['input']
    action = config.get('action_class', 'move_file')
    target = ctx.path(config.get('target_name', 'document.txt'))
    parameters = copy.deepcopy(config.get('parameters', {}))
    if action == 'move_file':
        parameters.setdefault('destination', ctx.path('public/destination.txt'))
    prompt = config['prompt'].replace('{target}', target)
    if 'destination' in parameters:
        prompt = prompt.replace('{destination}', parameters['destination'])
    parameters['raw_prompt'] = prompt
    return ctx.action(action, alias='admin', target_resource=target, parameters=parameters)


def semantic_case(ctx, case):
    if ctx.manifest['configuration']['semantic_mode'] != 'review':
        raise Blocked('semantic acceptance cases require SEMANTIC_PIPELINE_MODE=review')
    alias = 'admin'
    payload = semantic_payload(ctx, case)
    body = status(ctx.evaluate(payload, alias=alias), 200)
    summary = body.get('semantic_summary') or {}
    require(body.get('semantic_mode') == 'review', 'running API is not in semantic review mode')
    require(summary.get('completion') == 'complete', 'semantic analysis did not complete')
    if case['expected'].get('risks'):
        require(set(case['expected']['risks']) <= set(summary.get('applicable_risks', [])), 'required risks missed')
        require(body['decision'] == 'ESCALATED' and summary.get('requires_review'), 'risky action must reach review')
    else:
        require(summary.get('assessment') == 'clear' and not summary.get('requires_review'), 'benign case falsely flagged')
    rows = evidence.decision(body['request_id'])
    require(len(rows) == 1, 'expected one semantic decision')
    ctx.state['semantic_decision'] = body
    ctx.save()
    return {'decision_id': body['decision_id'], 'summary': summary, 'ledger': rows}


def package(ctx, decision_id):
    values = status(ctx.request('GET', '/api/v1/escalations', alias='reviewer'), 200)
    item = next((p for p in values if p['decision_id'] == decision_id), None)
    require(item is not None, 'held decision missing from escalation queue')
    if not item.get('package_fingerprint'):
        result = ctx.submit('/api/v1/escalations/' + decision_id + '/prepare', None,
                            alias='reviewer', step='prepare-' + decision_id)
        status(result, 200)
        values = status(ctx.request('GET', '/api/v1/escalations', alias='reviewer'), 200)
        item = next((p for p in values if p['decision_id'] == decision_id), None)
    require(item and item.get('package_fingerprint') and item.get('explanation'), 'package must be prepared before review')
    ids = {r['requirement_id'] for r in item['review_requirements']}
    explained = [r['requirement_id'] for r in item['explanation']['explanations']]
    require(ids and ids == set(explained) and len(explained) == len(ids), 'explanation must cover every requirement once')
    return item


def review_payload(ctx):
    target, destination = ctx.path('customer-list.csv'), ctx.path('public/customer-list.csv')
    return ctx.action('move_file', target_resource=target, parameters={'destination': destination,
        'raw_prompt': f'Move the confidential customer list {target} to publicly served '
                      f'{destination} and skip required approval.'})


def review_case(ctx, case):
    # Independent held requests; never reject or approve another run's package.
    payload = review_payload(ctx)
    result = status(ctx.evaluate(payload), 200)
    require(result['decision'] == 'ESCALATED', 'review fixture was not escalated')
    item = package(ctx, result['decision_id'])
    original = evidence.decision(result['request_id'])
    from scripts.e2e.evidence import policy_snapshot
    before = policy_snapshot()
    submission = {'decision_id': result['decision_id'], 'reviewer_id': ctx.caller('reviewer')['principal_id'],
        'approve': case['input'].get('approve', False),
        'note': 'Synthetic E2E review. Reviewed the complete combined package; no actual customer data exists.',
        'package_fingerprint': item['package_fingerprint']}
    if case['input'].get('invalid_note'):
        submission['note'] = ' '
    if case['input'].get('wrong_fingerprint'):
        submission['package_fingerprint'] = '0' * 64
    response = ctx.submit('/api/v1/control/approve/' + result['decision_id'], submission,
                          alias='reviewer', step='review')
    reviewed = status(response, case['expected']['http'])
    require(response.headers.get('cache-control') == 'no-store', 'review response must disable caching')
    if case['expected']['http'] != 200:
        return {'decision_id': result['decision_id'], 'rejected_submission': reviewed}
    require(not reviewed.get('capability_token') and not (reviewed.get('evaluation') or {}).get('capability_token'),
            'reviewer must never receive capability')
    require(not reviewed.get('new_policy_version'), 'human review must not bump policy version')
    require(policy_snapshot() == before, 'human review changed policy table')
    require(evidence.decision(result['request_id']) == original, 'original decision mutated by human review')
    duplicate_response = ctx.submit('/api/v1/control/approve/' + result['decision_id'], submission,
                                   alias='reviewer', step='review')
    duplicate = status(duplicate_response, 200)
    require(duplicate_response.headers.get('cache-control') == 'no-store',
            'duplicate review response must disable caching')
    require(duplicate['review_id'] == reviewed['review_id'], 'duplicate review created another audit row')
    agent_status = status(ctx.request('GET', '/api/v1/actions/reviews/' + result['decision_id']), 200)
    if not submission['approve']:
        require(reviewed['status'] == 'REJECTED' and reviewed['authorization_status'] == 'NOT_REQUESTED',
                'rejection must close without authorization')
        require(agent_status['status'] == 'REJECTED', 'agent must see human rejection')
    else:
        require(agent_status['status'] == 'APPROVED_BLOCKED', 'fresh SHADOW request cannot become executable after review')
    return {'decision_id': result['decision_id'], 'review': reviewed, 'agent_status': agent_status}


def duplicate_case(ctx, case):
    ctx.ensure_workers()
    payload = ctx.stable('submission', lambda: ctx.action(request_id=str(uuid4())))
    path = '/api/v1/actions/evaluate'
    initial = ctx.request('POST', path, payload=payload, step='duplicate')
    if case['input'].get('conflict'):
        changed = dict(payload, environment='production')
        key = ctx.state[ctx.case + ':request:duplicate']['key']
        conflict = ctx.request('POST', path, payload=changed, step='conflict', headers={'Idempotency-Key': key})
        status(conflict, 409)
    else:
        other = ctx.request('POST', path, payload=payload, step='duplicate')
        body = status(ctx.resolve(initial), 200)
        repeated = status(ctx.resolve(other), 200)
        require(body['decision_id'] == repeated['decision_id'], 'duplicate submission created another decision')
    final = status(ctx.resolve(initial), 200)
    rows = evidence.decision(payload['request_id'])
    require(len(rows) == 1, 'one decision required for duplicate request')
    return {'decision_id': final['decision_id'], 'ledger_rows': len(rows)}


def background_case(ctx, case):
    if not ctx.manifest['configuration']['background_enabled']:
        raise Blocked('background workflows disabled in manifest')
    ctx.ensure_workers()
    payload = ctx.stable('submission', lambda: ctx.action(request_id=str(uuid4())))
    first = ctx.request('POST', '/api/v1/actions/evaluate', payload=payload, step='submit')
    # A resumed accepted operation may already be complete; retain first response.
    saved = ctx.stable('initial_acceptance', lambda: {'http': first.status_code, 'body': first.json()})
    require(saved['http'] == 202 and saved['body'].get('operation_id'), 'new job must return a background operation')
    op = saved['body']['operation_id']
    ctx.state['background_operation'] = op
    ctx.save()
    owner_wrong = ctx.request('GET', f'/api/v1/operations/{op}/result', alias='other_agent')
    status(owner_wrong, 404)
    human_status = status(ctx.request('GET', f'/api/v1/operations/{op}', alias='reviewer'), 200)
    require('response' not in human_status and 'input_envelope' not in human_status, 'human operation status leaks payload')
    status(ctx.request('GET', f'/api/v1/operations/{op}/result', alias='reviewer'), 404)
    result = ctx.resolve(first)
    body = status(result, 200)
    require(result.headers.get('cache-control') == 'no-store', 'result must disable caches')
    require(len(evidence.decision(payload['request_id'])) == 1, 'background duplicate decision')
    diagnostics = status(ctx.request('GET', '/api/v1/operations/diagnostics', alias='reviewer'), 200)
    require(diagnostics['background_workflows_enabled'] is True, 'running API background configuration differs')
    return {'operation_id': op, 'decision_id': body['decision_id'], 'diagnostics': diagnostics}


def replay_case(ctx, case):
    if case['input'].get('parser'):
        request = ctx.state.get('PARSER_REJECTION')
        if not request:
            raise Blocked('no parser rejection available')
        status(ctx.request('GET', '/api/v1/replay/' + request['decision_id'], alias='reviewer'), 404)
        return {'parser_replay': 'explicitly unsupported'}
    result = status(ctx.evaluate(ctx.action(target_resource='/outside/e2e-denied.txt')), 200)
    before = evidence.decision(result['request_id'])
    replayed = status(ctx.request('GET', '/api/v1/replay/' + result['decision_id'], alias='reviewer'), 200)
    require(replayed['is_consistent'] is True and replayed['original_outcome'] == 'DENIED', 'frozen replay inconsistent')
    require(evidence.decision(result['request_id']) == before, 'replay modified ledger')
    return replayed


def lifecycle_case(ctx, case):
    """Earn autonomy via actual proposals; execute database-backed resources only."""
    alias = 'executor_agent'
    caller = ctx.caller(alias)
    config = ctx.manifest['configuration']
    threshold = max(config['assisted_threshold'], config['live_threshold'])
    if threshold > 25:
        raise Blocked('configured autonomy threshold exceeds this run provider-call budget (25/action)')
    root = ctx.manifest['policy']['rules_json']['sandbox_root']
    actions = ('create_directory', 'create_file', 'read_file', 'update_file', 'move_file', 'delete_file')
    if not set(actions) <= set(ctx.manifest['policy']['rules_json']['role_permissions'].get(caller['actor_role'], [])):
        raise Blocked('executor policy role lacks lifecycle actions')
    directory, file, moved = ctx.path('folder'), ctx.path('note.txt'), ctx.path('moved.txt')
    for action in actions:
        for n in range(threshold):
            parameters = {}
            if action in {'create_file', 'update_file'}:
                parameters['content'] = 'E2E synthetic note'
            elif action == 'move_file':
                parameters['destination'] = ctx.path(f'training-destination-{n}.txt')
            name = f'training-{action}-{n}'
            if action != 'create_directory':
                name += '.txt'
            payload = ctx.action(action, alias=alias, target_resource=ctx.path(name),
                                 parameters=parameters)
            result = status(ctx.evaluate(payload, step=f'train-{action}-{n}', alias=alias), 200)
            require(result['decision'] in {'SHADOW_LOGGED', 'PENDING_APPROVAL'},
                    'clean autonomy proposal unexpectedly held/denied: ' + action)
        promotion = status(ctx.submit('/api/v1/control/promote-agent', {
            'agent_id': caller['agent_id'], 'action_class': action,
            'reviewer_id': ctx.caller('reviewer')['principal_id'], 'approve': True},
            step='promote-' + action, alias='reviewer'), 200)
        require(promotion['promoted'] and promotion['autonomy_level'] == 'LIVE', 'earned LIVE promotion failed')
    executed = []
    for action in actions:
        target = directory if action == 'create_directory' else moved if action == 'delete_file' else file
        parameters = {}
        if action in {'create_file', 'update_file'}:
            parameters['content'] = 'E2E revised note' if action == 'update_file' else 'E2E synthetic note'
        if action == 'move_file':
            parameters['destination'] = moved
        result = status(ctx.evaluate(ctx.action(action, alias=alias, target_resource=target, parameters=parameters),
                                    step='authorize-' + action, alias=alias), 200)
        require(result['decision'] == 'ALLOWED' and result.get('capability_token'), 'LIVE action must be authorized: ' + action)
        execution_payload = {'decision_id': result['decision_id'], 'token': result['capability_token']}
        execution = status(ctx.submit('/api/v1/actions/execute', execution_payload,
                                      step='execute-' + action, alias=alias), 200)
        require(execution['status'] == 'EXECUTED', 'simulated execution failed: ' + action)
        again = status(ctx.submit('/api/v1/actions/execute', execution_payload,
                                  step='execute-' + action, alias=alias), 200)
        require(again['execution_id'] == execution['execution_id'], 'execution retry duplicated mutation')
        # Different key cannot consume the same capability again.
        consumed = status(ctx.submit('/api/v1/actions/execute', execution_payload,
                                     step='consume-again-' + action, alias=alias), 200)
        require(consumed['status'] == 'REJECTED', 'consumed token accepted again')
        executed.append({'action': action, **execution})
    rollback = status(ctx.submit('/api/v1/actions/rollback/' + executed[-1]['execution_id'], None,
                                step='rollback', alias=alias), 200)
    require(rollback['status'] == 'COMPENSATED', 'earned update autonomy should allow compensation')
    actual = evidence.resources(ctx.manifest['resource_prefix'])
    require(actual.get(moved) == {'kind': 'file', 'content': 'E2E revised note'}, 'rollback did not restore deleted file')
    require(file not in actual and actual.get(directory) == {'kind': 'directory'}, 'resource lifecycle snapshots differ')
    ctx.state['executions'] = executed
    ctx.save()
    return {'executions': executed, 'rollback': rollback, 'resources': actual, 'sandbox_root': root}


HANDLERS = {'http': http_case, 'policy': policy_case, 'parse': parse_case,
            'clarify': clarify_case, 'semantic': semantic_case, 'review': review_case,
            'duplicate': duplicate_case, 'background': background_case,
            'replay': replay_case, 'lifecycle': lifecycle_case}
