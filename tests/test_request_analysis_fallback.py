"""Purpose routes choose the configured adapter/model and keep fallback bounded."""
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.models.semantic import ActionMention, RequestAnalysis, JudgeAssessment, RiskCategory
from src.services import llm_routing, monitoring, request_analysis


class ProviderError(Exception):
    def __init__(self, status_code: int):
        self.status_code = status_code


SOURCE = 'Original instruction:\nRead /srv/agent/workspace/notes.txt.'


def test_judge_context_preserves_evidence_and_failures_without_mutating_audit():
    from copy import deepcopy
    from src.services.semantic_judge import judge_retrieval_context

    match = {'example_id': 'selected', 'query_id': 'risk-1', 'rank': 1, 'score': 0.8}
    retrieval = {
        'status': 'partial', 'collection': 'corpus', 'corpus_hash': 'hash',
        'embedding_spec': 'embedding', 'text_format': 'format', 'version': 'retrieval-v1',
        'coverage_gaps': ['query_unavailable:risk-1:benign'], 'provisional': True,
        'candidates': [{'example_id': 'selected', 'instruction': 'Complete source example.',
                        'label': 'risky', 'risk_aspects': ['credential_access'],
                        'findings': [{'category': 'credential_access', 'assessment': 'applies',
                                      'reason': 'Complete evidence rationale.'}],
                        'review_status': 'pending_human_review', 'matches': [match]}],
        'queries': [{'id': 'risk-1', 'text': 'Grounded query', 'purpose': 'risk',
                     'status': 'partial', 'matches': [match],
                     'searches': [{'label': 'risky', 'status': 'applied', 'matches': [match]},
                                  {'label': 'benign', 'status': 'unavailable', 'matches': [],
                                   'failure': {'code': 'query_unavailable'}}]}],
    }
    saved = deepcopy(retrieval)
    context = judge_retrieval_context(retrieval)
    assert retrieval == saved
    assert context['candidates'] == saved['candidates']
    assert context['coverage_gaps'] == saved['coverage_gaps']
    assert context['provisional'] is True and context['status'] == 'partial'
    assert not {'collection', 'corpus_hash', 'embedding_spec', 'text_format'} & context.keys()
    expected_query = deepcopy(saved['queries'][0])
    expected_query.pop('matches')
    for search in expected_query['searches']:
        search.pop('matches')
    assert context['queries'] == [expected_query]
    assert judge_retrieval_context({'candidates': []}) == {'candidates': []}


def good_analysis():
    return RequestAnalysis(
        actions=[ActionMention(action_class='read_file', excerpt='Read /srv/agent/workspace/notes.txt')],
        intent_summary='Read the named file.',
    )


@pytest.fixture(autouse=True)
def routing_fixture(monkeypatch):
    # Keep regression scenarios independent of the operator's active models.
    data = llm_routing.current_routing().model_dump()
    for purpose in ('request_parser', 'request_analyzer'):
        data['purposes'][purpose]['models'] = ['aicredits_safeguard', 'aicredits_qwen_flash']
        data['purposes'][purpose]['max_output_attempts'] = 3
    data['purposes']['semantic_judge']['models'] = ['gemini_lite']
    data['purposes']['semantic_judge']['max_output_attempts'] = 1
    routing = llm_routing.RoutingConfig.model_validate(data)
    monkeypatch.setattr(llm_routing, 'current_routing', lambda: routing)


def test_analyzer_uses_second_named_profile_on_5xx(monkeypatch):
    calls = []
    def make_parser(schema, profile, purpose):
        def invoke(messages):
            calls.append((profile.name, profile.provider, profile.model, messages[0]['content']))
            if len(calls) == 1:
                raise ProviderError(503)
            return good_analysis().model_dump()
        return SimpleNamespace(invoke=invoke)
    monkeypatch.setattr(llm_routing, 'make_parser', make_parser)
    attempt, token = monitoring.begin()
    try:
        result = request_analysis.analyze(SOURCE)
    finally:
        monitoring.end(token)
    assert [call[0] for call in calls] == ['aicredits_safeguard', 'aicredits_qwen_flash']
    assert calls[0][3] == request_analysis.SYSTEM
    assert calls[1][3] == request_analysis.SYSTEM
    assert result['model'] == 'qwen/qwen3.8-flash'
    assert result['fallback_from'] == dict(provider='aicredits',
        model='openai/gpt-oss-safeguard-20b', http_status=503)
    assert [event['stage'] for event in attempt.events] == ['analyzer_primary', 'analyzer', 'analyzer']
    assert result['output_attempts'] == 1
    assert result['provider_calls'] == 2


def test_nontransient_error_stops_before_second_profile(monkeypatch):
    calls = []
    def make_parser(schema, profile, purpose):
        def invoke(messages):
            calls.append(profile.name)
            raise ProviderError(400)
        return SimpleNamespace(invoke=invoke)
    monkeypatch.setattr(llm_routing, 'make_parser', make_parser)
    result = request_analysis.analyze(SOURCE)
    assert calls == ['aicredits_safeguard']
    assert result['status'] == 'unavailable'
    assert result['http_status'] == 400
    assert result['fallback_from'] is None


def test_parser_stops_when_current_output_attempt_exhausts_provider_retries(monkeypatch):
    from src.services.parser_workflow import run_parser_workflow
    calls = []
    def make_parser(schema, profile, purpose):
        def invoke(messages):
            calls.append(profile.name)
            raise ProviderError(503)
        return SimpleNamespace(invoke=invoke)
    monkeypatch.setattr(llm_routing, 'make_parser', make_parser)
    parser = llm_routing.RoutedStructuredParser('request_parser', RequestAnalysis)
    with pytest.raises(ProviderError) as caught:
        run_parser_workflow(
            parser=parser, messages=[{'role': 'user', 'content': SOURCE}],
            parse_value=lambda value: (RequestAnalysis.model_validate(value), None),
            validate_action=lambda value: None,
            classify_validation_error=lambda exc: 'retry',
            max_output_attempts=parser.purpose.max_output_attempts,
            max_provider_retries_per_attempt=1,
            retry_provider_error=parser.can_retry)
    assert calls == ['aicredits_safeguard', 'aicredits_qwen_flash']
    assert caught.value.parser_output_attempts == 1
    assert parser.calls == 2


def test_routing_rejects_unknown_model_and_bad_provider_adapter():
    data = llm_routing.current_routing().model_dump()
    data['purposes']['semantic_judge']['models'] = ['missing_profile']
    with pytest.raises(ValidationError, match='unknown model'):
        llm_routing.RoutingConfig.model_validate(data)
    data = llm_routing.current_routing().model_dump()
    data['providers']['gemini']['base_url_setting'] = 'AI_CREDITS_API_URL'
    with pytest.raises(ValidationError, match='does not use a base URL'):
        llm_routing.RoutingConfig.model_validate(data)


def test_named_profile_uses_provider_adapter(monkeypatch):
    from src.services.llm_providers import structured_parser
    purpose, profiles, _ = llm_routing.resolve_purpose('semantic_judge')
    profile = profiles[0]
    assert profile.adapter == 'google_genai'
    parser = structured_parser(JudgeAssessment, provider=profile.provider, model=profile.model)
    assert parser.adapter == 'google_genai'
    assert parser.model == profile.model


def test_judge_fallback_records_actual_responding_model(monkeypatch):
    from src.services import semantic_judge
    from src.models.semantic import JudgeAssessment
    data = llm_routing.current_routing().model_dump()
    data['purposes']['semantic_judge']['models'] = ['gemini_lite', 'aicredits_qwen_flash']
    data['purposes']['semantic_judge']['max_output_attempts'] = 2
    routing = llm_routing.RoutingConfig.model_validate(data)
    monkeypatch.setattr(llm_routing, 'current_routing', lambda: routing)
    calls = []
    def make_parser(schema, profile, purpose):
        def invoke(messages):
            calls.append(profile.name)
            if len(calls) == 1:
                raise ProviderError(503)
            return JudgeAssessment(findings=[], coverage_complete=True,
                                   explanation='No specified risks.').model_dump()
        return SimpleNamespace(invoke=invoke)
    monkeypatch.setattr(llm_routing, 'make_parser', make_parser)
    attempt, token = monitoring.begin()
    try:
        result = semantic_judge.judge(SOURCE,
            {'result':good_analysis().model_dump()}, {'candidates':[]})
    finally:
        monitoring.end(token)
    assert calls == ['gemini_lite', 'aicredits_qwen_flash']
    assert result['status'] == 'applied'
    assert result['provider'] == 'aicredits'
    assert result['model'] == 'qwen/qwen3.8-flash'
    assert [e['stage'] for e in attempt.events] == ['judge_primary', 'judge']


def test_provider_registry_supports_another_openai_gateway(monkeypatch):
    from src.services import llm_providers
    data = llm_routing.current_routing().model_dump()
    data['providers']['other_gateway'] = {
        'adapter':'openai_compatible', 'api_key_setting':'TEST_GATEWAY_KEY',
        'base_url_setting':'TEST_GATEWAY_URL'}
    data['models']['other_model'] = {'provider':'other_gateway', 'model':'test/model'}
    data['purposes']['semantic_judge']['models'] = ['other_model']
    routing = llm_routing.RoutingConfig.model_validate(data)
    monkeypatch.setattr(llm_routing, 'current_routing', lambda: routing)
    monkeypatch.setattr(llm_providers, 'current_routing', lambda: routing)
    monkeypatch.setenv('TEST_GATEWAY_KEY', 'test-key')
    monkeypatch.setenv('TEST_GATEWAY_URL', 'https://example.invalid/v1')
    profile = llm_routing.resolve_purpose('semantic_judge')[1][0]
    parser = llm_providers.structured_parser(JudgeAssessment, provider=profile.provider, model=profile.model)
    assert parser.adapter == 'openai_compatible'
    assert parser.base_url == 'https://example.invalid/v1'


def test_parser_graph_uses_second_profile_and_records_actual_model(monkeypatch):
    from src.config import settings
    from src.models.request import NaturalLanguageRequest
    from src.services import llm_parser
    monkeypatch.setattr(settings, 'OFFLINE_MODE', False)
    monkeypatch.setattr(settings, 'SEMANTIC_PIPELINE_MODE', 'baseline')
    calls = []
    def make_parser(schema, profile, purpose):
        def invoke(messages):
            calls.append(profile.name)
            if len(calls) == 1:
                raise ProviderError(503)
            return {'action_class':'read_file',
                    'target_resource':'/srv/agent/workspace/notes.txt',
                    'parameters':{}, 'environment':'production'}
        return SimpleNamespace(invoke=invoke)
    monkeypatch.setattr(llm_routing, 'make_parser', make_parser)
    result = llm_parser.parse(NaturalLanguageRequest(agent_id='test',
        actor_role='workspace_agent',
        raw_prompt='Read /srv/agent/workspace/notes.txt.'))
    provenance = result.parameters['parser_provenance']
    assert calls == ['aicredits_safeguard', 'aicredits_qwen_flash']
    assert provenance['source'] == 'aicredits:qwen/qwen3.8-flash'
    assert provenance['fallback_reason']['model'] == 'openai/gpt-oss-safeguard-20b'
    assert provenance['attempts'] == 2


def test_routing_allows_profiles_that_may_not_be_reached_within_provider_budget():
    data = llm_routing.current_routing().model_dump()
    data['purposes']['semantic_judge']['models'] = ['gemini_lite', 'aicredits_qwen_flash']
    data['purposes']['semantic_judge']['max_provider_retries_per_attempt'] = 0
    routing = llm_routing.RoutingConfig.model_validate(data)
    assert len(routing.purposes['semantic_judge'].models) == 2


def test_standalone_enforces_budget_even_if_config_mutated(monkeypatch):
    purpose, profiles, policy = llm_routing.resolve_purpose('request_analyzer')
    purpose = purpose.model_copy(update={'max_output_attempts': 1, 'max_provider_retries_per_attempt': 0})
    monkeypatch.setattr(llm_routing, 'resolve_purpose', lambda _: (purpose, profiles, policy))
    calls = []
    def make_parser(schema, profile, purpose):
        def invoke(messages):
            calls.append(profile.name)
            raise ProviderError(503)
        return SimpleNamespace(invoke=invoke)
    monkeypatch.setattr(llm_routing, 'make_parser', make_parser)
    with pytest.raises(ProviderError):
        llm_routing.invoke_for_purpose('request_analyzer', RequestAnalysis, lambda _: [])
    assert calls == ['aicredits_safeguard']


def test_parser_graph_respects_yaml_retry_policy(monkeypatch):
    from src.config import settings
    from src.models.request import NaturalLanguageRequest
    from src.services import llm_parser
    monkeypatch.setattr(settings, 'OFFLINE_MODE', False)
    monkeypatch.setattr(settings, 'SEMANTIC_PIPELINE_MODE', 'baseline')
    routing = llm_routing.current_routing().model_copy(deep=True)
    routing.fallback_policies['transient'].http_statuses = []
    monkeypatch.setattr(llm_routing, 'current_routing', lambda: routing)
    calls = []
    def make_parser(schema, profile, purpose):
        def invoke(messages):
            calls.append(profile.name)
            raise ProviderError(503)
        return SimpleNamespace(invoke=invoke)
    monkeypatch.setattr(llm_routing, 'make_parser', make_parser)
    with pytest.raises(Exception):
        llm_parser.parse(NaturalLanguageRequest(agent_id='test',
            actor_role='workspace_agent', raw_prompt='Read /srv/agent/workspace/notes.txt.'))
    assert calls == ['aicredits_safeguard']


def test_json_only_analysis_correction_records_action_coverage_error(monkeypatch):
    from copy import deepcopy
    from src.models.request import ActionRequest
    source = request_analysis.source_text(ActionRequest(agent_id='validator-test',
        actor_role='workspace_agent', action_class='create_file',
        target_resource='/srv/agent/workspace/note.txt', parameters={'content': 'Read another file.'}))
    replies = [
        {'intent_summary': 'Create the submitted file.', 'actions': []},
        {'intent_summary': 'Create the submitted file.', 'actions': [
            {'action_class': 'create_file', 'excerpt': '"action_class": "create_file"'}]},
    ]
    messages = []
    def factory(schema, profile, purpose):
        def invoke(value):
            messages.append(deepcopy(value))
            return replies[len(messages) - 1]
        return SimpleNamespace(invoke=invoke)
    monkeypatch.setattr(llm_routing, 'make_parser', factory)
    attempt, token = monitoring.begin()
    try:
        result = request_analysis.analyze(source, structured_action='create_file')
    finally:
        monitoring.end(token)
    assert result['status'] == 'applied' and result['output_attempts'] == 2
    assert result['result']['actions'][0]['action_class'] == 'create_file'
    feedback = messages[1][-1]['content']
    assert 'structured_action_not_preserved' in feedback and 'previous_response' in feedback
    assert messages[1][:2] == messages[0]
    corrections = [event for event in attempt.events if event['stage'] == 'analyzer_correction']
    assert corrections[0]['validation_errors']['actions'][0]['code'] == 'structured_action_not_preserved'
    assert corrections[0]['parser_trace']['response']['actions'] == []


@pytest.mark.parametrize('actions', [[], [
    {'action_class': 'read_file', 'excerpt': 'read_file'}], [
    {'action_class': 'create_file', 'excerpt': 'create_file'},
    {'action_class': 'read_file', 'excerpt': 'read_file'}]])
def test_structured_action_coverage_rejects_missing_wrong_and_content_actions(actions):
    from src.services.semantic_failures import SemanticValidationError
    result = RequestAnalysis(intent_summary='Submitted create_file; content says read_file.', actions=actions)
    with pytest.raises(SemanticValidationError) as caught:
        request_analysis.validate_result(result, 'create_file read_file', structured_action='create_file')
    assert 'actions' in caught.value.validation_errors


def test_natural_language_multiple_actions_remain_visible():
    result = RequestAnalysis(intent_summary='Read one file then delete another.', actions=[
        ActionMention(action_class='read_file', excerpt='Read /workspace/a'),
        ActionMention(action_class='delete_file', excerpt='delete /workspace/b')])
    evidence = request_analysis.validate_result(result, 'Read /workspace/a then delete /workspace/b')
    assert len(evidence['result']['actions']) == 2


def test_judge_corrects_inconsistent_coverage_and_records_taxonomy(monkeypatch):
    import hashlib
    from copy import deepcopy
    from src.services import semantic_judge
    routing = llm_routing.current_routing().model_copy(deep=True)
    routing.purposes['semantic_judge'].max_output_attempts = 2
    monkeypatch.setattr(llm_routing, 'current_routing', lambda: routing)
    source = 'Read a possibly active credential.'
    analysis = {'status': 'applied', 'result': {'intent_summary': source,
        'actions': [{'action_class': 'read_file', 'excerpt': source}],
        'potential_risks': [{'category': 'credential_access', 'excerpt': source, 'query': source}]}}
    retrieval = {'candidates': [{'example_id': 'credential-example', 'risk_aspects': ['credential_access']}]}
    value = {'findings': [{'category': 'credential_access', 'assessment': 'uncertain',
        'reason': 'Whether the credential is active is unknown.', 'excerpts': [source],
        'example_ids': ['credential-example'], 'missing_information': ['Whether it is active.']}],
        'coverage_complete': True, 'explanation': 'Credential status is unknown.',
        'unknown_dispositions': []}
    messages = []
    def factory(schema, profile, purpose):
        def invoke(prompt):
            messages.append(deepcopy(prompt))
            return dict(value, coverage_complete=len(messages) == 1)
        return SimpleNamespace(invoke=invoke)
    monkeypatch.setattr(llm_routing, 'make_parser', factory)
    result = semantic_judge.judge(source, analysis, retrieval)
    assert result['status'] == 'applied' and result['output_attempts'] == 2
    assert result['result']['findings'][0]['assessment'] == 'uncertain'
    assert result['result']['coverage_complete'] is False
    assert 'complete_with_unresolved_assessment' in messages[1][-1]['content']
    assert 'Handling known active authentication material' in messages[0][0]['content']
    assert result['prompt_hash'] == hashlib.sha256(messages[0][0]['content'].encode()).hexdigest()
    assert set(result['taxonomy']['risk_categories']) == {c.value for c in RiskCategory}
    semantic_judge.validate_taxonomy_snapshot(result)
    with pytest.raises(ValueError, match='fingerprint'):
        semantic_judge.validate_taxonomy_snapshot(dict(result, taxonomy_hash='0' * 64))
    # An older frozen judgment keeps its previous validation semantics.
    semantic_judge.validate_result(value, analysis, retrieval, source=source)
    from src.services.semantic_failures import SemanticValidationError
    with pytest.raises(SemanticValidationError):
        semantic_judge.validate_result(value, analysis, retrieval, source=source, strict_consistency=True)
    # A prompt-version bump must not weaken previously saved v3 evidence.
    for version in semantic_judge.STRICT_EVIDENCE_VERSIONS:
        frozen = dict(result, version=version)
        semantic_judge.validate_frozen_result(frozen, analysis, retrieval, source=source)
        with pytest.raises(ValueError, match='fingerprint'):
            semantic_judge.validate_frozen_result(dict(frozen, taxonomy_hash='0' * 64),
                                                analysis, retrieval, source=source)
        with pytest.raises(SemanticValidationError):
            semantic_judge.validate_frozen_result(dict(frozen, result=value),
                                                analysis, retrieval, source=source)
    for version in ('risk-judge-v1', 'risk-judge-v2'):
        semantic_judge.validate_frozen_result({'version': version, 'result': value},
                                            analysis, retrieval, source=source)


def test_judge_rejects_hypothetical_overwrite_risk_for_plain_move(monkeypatch):
    from src.services import semantic_judge
    from src.services.semantic_failures import SemanticValidationError
    instruction = 'Move /workspace/draft.txt to /workspace/reviewed.txt. Both are public drafts.'
    source = ('Original instruction:\n' + instruction
              + '\nAuthoritative action JSON:\n{"action_class":"move_file"}')
    analysis = {'status': 'applied', 'result': {'intent_summary': instruction,
        'actions': [{'action_class': 'move_file', 'excerpt': instruction}],
        'potential_risks': [], 'unknowns': [], 'unmapped_concerns': []}}
    retrieval = {'candidates': [{'example_id': 'overwrite-example',
        'risk_aspects': ['destructive_overwrite']}]}
    value = {'findings': [{'category': 'destructive_overwrite', 'assessment': 'uncertain',
        'reason': 'The destination may already exist.', 'excerpts': [instruction],
        'example_ids': ['overwrite-example'],
        'missing_information': ['Whether the destination exists.']}],
        'coverage_complete': False, 'explanation': 'Destination state is unknown.',
        'unknown_dispositions': []}

    with pytest.raises(SemanticValidationError) as caught:
        semantic_judge.validate_result(value, analysis, retrieval, source=source,
            strict_consistency=True, validate_action_effects=True)
    assert caught.value.validation_errors['findings.0'][0]['code'] == 'unsupported_move_overwrite_finding'

    assert 'rejects a move when its destination exists' in caught.value.validation_errors['findings.0'][0]['message']

    # Existing frozen judgments retain their original validation behavior.
    semantic_judge.validate_result(value, analysis, retrieval, source=source,
                                   strict_consistency=True)
    import hashlib
    import json
    from src.services.semantic_v3_corpus import load_taxonomy
    taxonomy = load_taxonomy()
    frozen = {'version': 'risk-judge-v4', 'result': value, 'taxonomy': taxonomy,
        'taxonomy_hash': hashlib.sha256(json.dumps(taxonomy, sort_keys=True).encode()).hexdigest()}
    semantic_judge.validate_frozen_result(frozen, analysis, retrieval, source=source)
    with pytest.raises(SemanticValidationError) as caught:
        semantic_judge.validate_frozen_result(dict(frozen, version='risk-judge-v5'),
            analysis, retrieval, source=source)
    assert caught.value.validation_errors['findings.0'][0]['code'] == 'unsupported_move_overwrite_finding'

    # A future prompt revision must retain v5's saved action-effect contract.
    monkeypatch.setattr(semantic_judge, 'VERSION', 'risk-judge-future')
    with pytest.raises(SemanticValidationError):
        semantic_judge.validate_frozen_result(dict(frozen, version='risk-judge-v5'),
            analysis, retrieval, source=source)


def test_judge_rejects_move_overwrite_uncertainty_when_user_negated_overwrite():
    from src.services import semantic_judge
    from src.services.semantic_failures import SemanticValidationError
    instruction = 'Move /workspace/draft.txt to /workspace/reviewed.txt, but do not overwrite anything.'
    source = ('Original instruction:\n' + instruction
              + '\nAuthoritative action JSON:\n{"action_class":"move_file"}')
    analysis = {'status': 'applied', 'result': {'intent_summary': instruction,
        'actions': [{'action_class': 'move_file', 'excerpt': instruction}],
        'potential_risks': [], 'unknowns': [], 'unmapped_concerns': []}}
    retrieval = {'candidates': [{'example_id': 'overwrite-example',
        'risk_aspects': ['destructive_overwrite'],
        'findings': [{'category': 'destructive_overwrite', 'assessment': 'does_not_apply'}]}]}
    value = {'findings': [{'category': 'destructive_overwrite', 'assessment': 'uncertain',
        'reason': 'The destination may already exist.', 'excerpts': [instruction],
        'example_ids': ['overwrite-example'],
        'missing_information': ['Whether the destination exists.']}],
        'coverage_complete': False, 'explanation': 'Destination state is unknown.',
        'unknown_dispositions': []}
    with pytest.raises(SemanticValidationError) as caught:
        semantic_judge.validate_result(value, analysis, retrieval, source=source,
            strict_consistency=True, validate_action_effects=True)
    assert caught.value.validation_errors['findings.0'][0]['code'] == 'unsupported_move_overwrite_finding'


def test_judge_corrects_hypothetical_move_overwrite_using_bounded_feedback(monkeypatch):
    from copy import deepcopy
    from src.services import semantic_judge
    routing = llm_routing.current_routing().model_copy(deep=True)
    routing.purposes['semantic_judge'].max_output_attempts = 2
    monkeypatch.setattr(llm_routing, 'current_routing', lambda: routing)
    instruction = 'Move /workspace/draft.txt to /workspace/reviewed.txt. Both are public drafts.'
    source = ('Original instruction:\n' + instruction
              + '\nAuthoritative action JSON:\n{"action_class":"move_file"}')
    analysis = {'status': 'applied', 'result': {'intent_summary': instruction,
        'actions': [{'action_class': 'move_file', 'excerpt': instruction}],
        'potential_risks': [], 'unknowns': [], 'unmapped_concerns': []}}
    retrieval = {'candidates': [{'example_id': 'overwrite-example',
        'risk_aspects': ['destructive_overwrite'],
        'findings': [{'category': 'destructive_overwrite', 'assessment': 'does_not_apply'}]}]}
    finding = {'category': 'destructive_overwrite', 'assessment': 'uncertain',
        'reason': 'The destination may already exist.', 'excerpts': [instruction],
        'example_ids': ['overwrite-example'],
        'missing_information': ['Whether the destination exists.']}
    invalid = {'findings': [finding], 'coverage_complete': False,
        'explanation': 'Destination state is unknown.', 'unknown_dispositions': []}
    corrected = deepcopy(invalid)
    corrected['findings'][0].update(assessment='does_not_apply',
        reason='A plain move does not replace an existing destination.', missing_information=[])
    corrected['coverage_complete'] = True
    corrected['explanation'] = 'No supported overwrite risk applies to this move.'
    messages = []

    def factory(schema, profile, purpose):
        def invoke(prompt):
            messages.append(deepcopy(prompt))
            return invalid if len(messages) == 1 else corrected
        return SimpleNamespace(invoke=invoke)

    monkeypatch.setattr(llm_routing, 'make_parser', factory)
    result = semantic_judge.judge(source, analysis, retrieval)

    assert result['status'] == 'applied'
    assert result['version'] == semantic_judge.VERSION
    assert result['output_attempts'] == 2
    assert result['result']['findings'][0]['assessment'] == 'does_not_apply'
    correction = messages[1][-1]['content']
    assert 'unsupported_move_overwrite_finding' in correction
    assert 'This application rejects a move when its destination exists' in correction


@pytest.mark.parametrize('instruction', [
    'Move /workspace/draft.txt to /workspace/reviewed.txt and overwrite the old copy.',
    'Move /workspace/draft.txt to /workspace/reviewed.txt; replace the current version.',
    'Move /workspace/draft.txt to /workspace/reviewed.txt and destroy its existing contents.',
])
def test_judge_preserves_explicit_replacement_concerns(instruction):
    from src.services import semantic_judge
    source = ('Original instruction:\n' + instruction
              + '\nAuthoritative action JSON:\n{"action_class":"move_file"}')
    analysis = {'status': 'applied', 'result': {'intent_summary': instruction,
        'actions': [{'action_class': 'move_file', 'excerpt': instruction}],
        'potential_risks': [], 'unknowns': [], 'unmapped_concerns': []}}
    retrieval = {'candidates': [{'example_id': 'overwrite-example',
        'risk_aspects': ['destructive_overwrite']}]}
    value = {'findings': [{'category': 'destructive_overwrite', 'assessment': 'uncertain',
        'reason': 'The request explicitly asks to replace an existing copy.',
        'excerpts': [instruction], 'example_ids': ['overwrite-example'],
        'missing_information': ['Whether the existing copy contains required information.']}],
        'coverage_complete': False, 'explanation': 'Importance of the existing copy is unknown.',
        'unknown_dispositions': []}
    semantic_judge.validate_result(value, analysis, retrieval, source=source,
        strict_consistency=True, validate_action_effects=True)


def test_semantic_fixtures_use_class_specific_arguments_and_consistent_paths():
    from scripts.e2e.registry import cases
    from scripts.e2e.scenarios import semantic_payload, review_payload
    from scripts.e2e.common import Context
    ctx = SimpleNamespace(path=lambda name: '/srv/agent/workspace/fixture/' + name,
        caller=lambda alias: {'agent_id': 'fixture-agent', 'actor_role': 'admin_agent'})
    ctx.action = lambda action, **kwargs: Context.action(ctx, action, **kwargs)
    allowed = {'create_file': {'content'}, 'read_file': set(), 'move_file': {'destination'},
               'delete_directory': {'recursive'}, 'update_file': {'content'}}
    rows = [row for row in cases() if row.get('handler') == 'semantic']
    assert len(rows) == 9
    for case in rows:
        payload = semantic_payload(ctx, case)
        action, params = payload['action_class'], payload['parameters']
        assert set(params) - {'raw_prompt'} <= allowed[action]
        assert '{target}' not in params['raw_prompt'] and '{destination}' not in params['raw_prompt']
        assert payload['target_resource'] in params['raw_prompt']
        if params.get('content'):
            assert params['content'] in params['raw_prompt']
        if action == 'move_file':
            assert params['destination'] in params['raw_prompt']
        else:
            assert 'destination' not in params
        if action == 'delete_directory':
            assert payload['target_resource'].endswith('/') and params['recursive'] is True
    for action in ['read_file', 'create_directory', 'delete_file', 'delete_directory']:
        assert ctx.action(action)['parameters'] == {}
    review = review_payload(ctx)
    assert review['target_resource'] == ctx.path('customer-list.csv')
    assert review['parameters']['destination'] == ctx.path('public/customer-list.csv')
    assert review['target_resource'] in review['parameters']['raw_prompt']
    assert review['parameters']['destination'] in review['parameters']['raw_prompt']


def test_judge_prompt_applies_materially_indicated_incident_record_definition():
    from src.services import semantic_judge
    from src.services.semantic_v3_corpus import load_taxonomy
    prompt = ' '.join(semantic_judge.system_prompt(load_taxonomy()).lower().split())
    assert 'incident register explicitly described as recording a breach' in prompt
    assert 'materially indicates an investigative record' in prompt
    assert 'even if the request does not separately name' in prompt


@pytest.mark.parametrize('approve', [False, True])
@pytest.mark.parametrize('http,first_header,duplicate_header,error', [
    (200, True, True, None),
    (200, False, True, 'review response must disable caching'),
    (200, True, False, 'duplicate review response must disable caching'),
    (422, True, False, None),
    (422, False, False, 'review response must disable caching'),
])
def test_review_scenario_checks_cache_headers_on_initial_duplicate_and_error(
        monkeypatch, approve, http, first_header, duplicate_header, error):
    import httpx
    from scripts.e2e import scenarios
    from scripts.e2e.common import Context
    ctx = SimpleNamespace(path=lambda name: '/srv/agent/workspace/fixture/' + name,
        caller=lambda alias: {'agent_id': 'fixture-agent', 'actor_role': 'workspace_agent',
                              'principal_id': 'fixture-reviewer'})
    ctx.action = lambda action, **kwargs: Context.action(ctx, action, **kwargs)
    ctx.evaluate = lambda payload: httpx.Response(200, json={
        'decision': 'ESCALATED', 'decision_id': 'decision', 'request_id': 'request'})
    reviewed = {'review_id': 'review', 'status': 'APPROVED_BLOCKED' if approve else 'REJECTED',
                'authorization_status': 'BLOCKED' if approve else 'NOT_REQUESTED'}
    responses = iter([
        httpx.Response(http, json=reviewed if http == 200 else {'detail': 'invalid submission'},
                       headers={'Cache-Control': 'no-store'} if first_header else {}),
        httpx.Response(200, json=reviewed,
                       headers={'Cache-Control': 'no-store'} if duplicate_header else {}),
    ])
    ctx.submit = lambda *args, **kwargs: next(responses)
    ctx.request = lambda *args, **kwargs: httpx.Response(200, json={'status': reviewed['status']})
    monkeypatch.setattr(scenarios, 'package', lambda *args: {'package_fingerprint': 'fingerprint'})
    monkeypatch.setattr(scenarios.evidence, 'decision', lambda *args: [])
    monkeypatch.setattr(scenarios.evidence, 'policy_snapshot', lambda: [])
    case = {'input': {'approve': approve}, 'expected': {'http': http}}
    if error:
        with pytest.raises(AssertionError, match=error):
            scenarios.review_case(ctx, case)
    else:
        scenarios.review_case(ctx, case)


@pytest.mark.parametrize('policy_lists_action', [False, True])
@pytest.mark.parametrize('actor_role', ['admin_agent', 'unlisted_role'])
def test_unsupported_runtime_action_is_audited_denial_without_cloud_work(
        monkeypatch, policy_lists_action, actor_role):
    import json
    from pathlib import Path
    from src.models.request import ActionRequest
    from src.models.policy import Policy
    from src.models.autonomy import AutonomyState
    from src.models.decision_contracts import PolicyDecisionWrite
    from src.models.request import EvaluationResponse
    from src.services import advisory_evidence, policy_engine, semantic_pipeline
    from src.services.policy_loader import canonical_policy_hash
    def no_cloud(*args, **kwargs):
        raise AssertionError('unsupported runtime action must not call a model or retrieval')
    monkeypatch.setattr(advisory_evidence.semantic_engine, 'assess', no_cloud)
    monkeypatch.setattr(request_analysis, 'analyze', no_cloud)
    request = ActionRequest(agent_id='validator-test', actor_role=actor_role,
        action_class='launch_rocket', target_resource='/srv/agent/workspace/rocket',
        environment='development')
    evidence = advisory_evidence.build_evidence(request)
    semantic_pipeline.verify_frozen(evidence)
    # Cover ordinary policies as well as policies listing the unknown operation.
    rules = json.loads((Path(__file__).parents[1] / 'policies/policy_v1.0.0.json').read_text())
    if policy_lists_action:
        rules['risk_map']['launch_rocket'] = 'LOW'
        rules['role_permissions']['admin_agent'].append('launch_rocket')
    policy = Policy(version=rules['version'], policy_hash=canonical_policy_hash(rules), rules_json=rules)
    state = AutonomyState(agent_id=request.agent_id, action_class=request.action_class, mode='LIVE')
    result = policy_engine.evaluate(request, policy, evidence, state)
    assert result.outcome.value == 'DENIED' and result.ttl_seconds is None
    assert result.reason == evidence['unsupported_action']['reason']
    payload = dict(request_id=request.request_id, agent_id=request.agent_id, actor_role=request.actor_role,
        request_json=request, action_class=request.action_class, target_resource=request.target_resource,
        policy_hash=policy.policy_hash, risk_level=result.risk_level, autonomy_level=result.autonomy_level,
        outcome=result.outcome, reason=result.reason, evidence_json=result.evidence)
    PolicyDecisionWrite.model_validate(payload)
    with pytest.raises(ValidationError, match='DENIED'):
        PolicyDecisionWrite.model_validate(dict(payload, outcome='ALLOWED'))
    response = EvaluationResponse(decision_id='decision-1', request_id=request.request_id,
        action_request=request, decision='DENIED', risk_level=result.risk_level,
        blast_radius=result.blast_radius, policy_hash=result.policy_hash, reason=result.reason,
        autonomy_level=result.autonomy_level,
        unsupported_action={'code': 'UNSUPPORTED_ACTION', 'action_class': request.action_class,
                            'reason': evidence['unsupported_action']['reason']})
    assert response.model_dump(mode='json')['unsupported_action']['code'] == 'UNSUPPORTED_ACTION'
    with pytest.raises(ValidationError, match='unsupported-action refusal'):
        EvaluationResponse.model_validate(response.model_dump(mode='json') | {
            'unsupported_action': {'code': 'UNSUPPORTED_ACTION', 'action_class': 'delete_file',
                                   'reason': result.reason}})
    # Previously stored v1 decisions must retain their exact reason on replay.
    from copy import deepcopy
    legacy = deepcopy(evidence)
    legacy['unsupported_action']['version'] = 'unsupported-action-v1'
    legacy['semantic']['source'] = 'unsupported-action-v1'
    legacy['blast_radius']['source'] = 'unsupported-action-v1'
    semantic_pipeline.verify_frozen(legacy)
    replayed = policy_engine.evaluate(request, policy, legacy, state)
    expected_reason = (
        f"role {actor_role!r} is not in the policy" if actor_role == 'unlisted_role'
        else f"action {request.action_class!r} is not in the policy risk_map" if not policy_lists_action
        else legacy['unsupported_action']['reason'])
    assert replayed.outcome.value == 'DENIED' and replayed.reason == expected_reason


def test_judge_definite_findings_require_category_and_assessment_support():
    from src.services import semantic_judge
    from src.services.semantic_failures import SemanticValidationError
    source = 'Read /srv/agent/workspace/credentials.txt.'
    analysis = {'result': {'intent_summary': source,
        'actions': [{'action_class': 'read_file', 'excerpt': source}],
        'potential_risks': [{'category': 'credential_access', 'excerpt': source,
                             'query': 'active credentials'}],
        'unknowns': [], 'unmapped_concerns': []}}
    retrieval = {'candidates': [{'example_id': 'benign-read',
        'risk_aspects': ['credential_access'],
        'findings': [{'category': 'credential_access', 'assessment': 'does_not_apply'}]}]}
    result = {'findings': [{'category': 'credential_access', 'assessment': 'applies',
        'excerpts': [source], 'example_ids': ['benign-read'], 'reason': 'The action reads credentials.',
        'missing_information': []}], 'coverage_complete': True,
        'unmapped_concerns': [], 'explanation': 'Credential access applies.',
        'unknown_dispositions': []}
    with pytest.raises(SemanticValidationError) as caught:
        semantic_judge.validate_result(result, analysis, retrieval, source=source,
            validate_citation_consistency=True)
    assert caught.value.validation_errors['findings.0.example_ids'][0]['code'] == 'citation_assessment_mismatch'


def source_dismissal_case(category='mass_deletion'):
    source = 'Create /workspace/public/hash.txt with the public checksum "DEMO_DIGEST".'
    analysis = {'result': {'intent_summary': source,
        'actions': [{'action_class': 'create_file', 'excerpt': source}],
        'potential_risks': [], 'unknowns': [], 'unmapped_concerns': []}}
    retrieval = {'candidates': [{'example_id': 'contrast', 'risk_aspects': [category],
        'findings': [{'category': category, 'assessment': 'applies'}]}]}
    value = {'findings': [{'category': category, 'assessment': 'does_not_apply',
        'excerpts': [source], 'example_ids': [],
        'reason': 'The requested behavior creates one file with explicitly public content; '
                  'it does not request this risk mechanism.', 'missing_information': []}],
        'coverage_complete': True, 'unmapped_concerns': [],
        'explanation': 'The requested operation rules out this risk.', 'unknown_dispositions': []}
    return source, analysis, retrieval, value


@pytest.mark.parametrize('category', [item.value for item in RiskCategory])
def test_current_judge_accepts_grounded_dismissals_without_category_matched_benign_examples(category):
    from src.services import semantic_judge
    source, analysis, retrieval, value = source_dismissal_case(category)
    # The explanation is model judgment; this exercises evidence requirements,
    # not proof of semantic correctness or of facts about the live filesystem.
    semantic_judge.validate_result(value, analysis, retrieval, source=source,
        strict_consistency=True, validate_citation_consistency=True, allow_source_dismissals=True)


def test_current_judge_accepts_a_same_category_risky_contrast_for_a_dismissal():
    from src.services import semantic_judge
    from src.services.semantic_failures import SemanticValidationError
    source, analysis, retrieval, value = source_dismissal_case()
    value['findings'][0].update(example_ids=['contrast'],
        reason='Unlike the deletion example, creating a public checksum file deletes nothing.')
    semantic_judge.validate_result(value, analysis, retrieval, source=source,
        validate_citation_consistency=True, allow_source_dismissals=True)
    with pytest.raises(SemanticValidationError) as caught:
        semantic_judge.validate_result(value, analysis, retrieval, source=source,
            validate_citation_consistency=True)
    assert caught.value.validation_errors['findings.0.example_ids'][0]['code'] == 'citation_assessment_mismatch'


@pytest.mark.parametrize('change,field,code', [
    ('unknown_id', 'example_ids', 'unknown_example_id'),
    ('unrelated_category', 'example_ids', 'citation_category_mismatch'),
    ('invented_excerpt', 'excerpts', 'excerpt_not_in_source'),
    ('blank_excerpt', 'excerpts', 'excerpt_not_in_source'),
    ('blank_reason', 'reason', 'missing_dismissal_reason'),
    ('missing_fact', 'missing_information', 'definite_finding_has_missing_information'),
])
def test_source_dismissals_retain_grounding_and_citation_checks(change, field, code):
    from src.services import semantic_judge
    from src.services.semantic_failures import SemanticValidationError
    source, analysis, retrieval, value = source_dismissal_case()
    finding = value['findings'][0]
    if change == 'unknown_id':
        finding['example_ids'] = ['invented']
    elif change == 'unrelated_category':
        retrieval['candidates'].append({'example_id': 'other', 'risk_aspects': ['secret_export'],
            'findings': [{'category': 'secret_export', 'assessment': 'applies'}]})
        # Keep every required category represented; only the citation is invalid.
        finding['example_ids'] = ['other']
        from copy import deepcopy
        other = deepcopy(finding)
        other.update(category='secret_export', example_ids=[])
        value['findings'].append(other)
    elif change == 'invented_excerpt':
        finding['excerpts'] = ['Nothing will be deleted.']
    elif change == 'blank_excerpt':
        finding['excerpts'] = [' ']
    elif change == 'blank_reason':
        finding['reason'] = ' '
    else:
        finding['missing_information'] = ['Whether this operation deletes files.']
    with pytest.raises(SemanticValidationError) as caught:
        semantic_judge.validate_result(value, analysis, retrieval, source=source,
            strict_consistency=True, validate_citation_consistency=True, allow_source_dismissals=True)
    assert caught.value.validation_errors['findings.0.' + field][0]['code'] == code


@pytest.mark.parametrize('citation,annotation,expected_error', [
    ([], 'applies', 'missing_evidence_reference'),
    (['contrast'], 'does_not_apply', 'citation_assessment_mismatch'),
    (['contrast'], 'uncertain', 'citation_assessment_mismatch'),
    (['contrast'], 'applies', None),
])
def test_positive_risks_still_require_matching_retrieved_support(citation, annotation, expected_error):
    from src.services import semantic_judge
    from src.services.semantic_failures import SemanticValidationError
    source, analysis, retrieval, value = source_dismissal_case('data_exposure')
    value['findings'][0].update(assessment='applies', example_ids=citation)
    retrieval['candidates'][0]['findings'][0]['assessment'] = annotation
    if expected_error is None:
        semantic_judge.validate_result(value, analysis, retrieval, source=source,
            validate_citation_consistency=True, allow_source_dismissals=True)
    else:
        with pytest.raises(SemanticValidationError) as caught:
            semantic_judge.validate_result(value, analysis, retrieval, source=source,
                validate_citation_consistency=True, allow_source_dismissals=True)
        assert caught.value.validation_errors['findings.0.example_ids'][0]['code'] == expected_error


def test_source_dismissal_contract_is_versioned_for_frozen_replay(monkeypatch):
    import hashlib
    import json
    from src.services import semantic_judge
    from src.services.semantic_failures import SemanticValidationError
    from src.services.semantic_v3_corpus import load_taxonomy
    source, analysis, retrieval, value = source_dismissal_case()
    taxonomy = load_taxonomy()
    envelope = {'version': semantic_judge.VERSION, 'result': value, 'taxonomy': taxonomy,
        'taxonomy_hash': hashlib.sha256(json.dumps(taxonomy, sort_keys=True).encode()).hexdigest()}
    # Neither a future prompt bump nor a changed on-disk taxonomy changes replay.
    monkeypatch.setattr(semantic_judge, 'VERSION', 'risk-judge-future')
    monkeypatch.setattr('src.services.semantic_v3_corpus.load_taxonomy',
                        lambda: pytest.fail('replay loaded the current taxonomy'))
    semantic_judge.validate_frozen_result(envelope, analysis, retrieval, source=source)
    for version in ('risk-judge-v2', 'risk-judge-v3', 'risk-judge-v7', 'risk-judge-v8', 'risk-judge-v9'):
        with pytest.raises(SemanticValidationError) as caught:
            semantic_judge.validate_frozen_result(dict(envelope, version=version),
                analysis, retrieval, source=source)
        assert caught.value.validation_errors['findings.0.example_ids'][0]['code'] == 'missing_evidence_reference'


def test_citation_correction_preserves_positive_risk_and_removes_only_unrelated_dismissal_reference(monkeypatch):
    from copy import deepcopy
    from src.services import semantic_judge
    source = 'Move /workspace/confidential.pdf to /workspace/public/confidential.pdf, readable by everyone.'
    analysis = {'result': {'intent_summary': source,
        'actions': [{'action_class': 'move_file', 'excerpt': source}],
        'potential_risks': [], 'unknowns': [], 'unmapped_concerns': []}}
    retrieval = {'candidates': [{'example_id': 'public-exposure', 'risk_aspects': ['data_exposure'],
        'findings': [{'category': 'data_exposure', 'assessment': 'applies'}]}]}
    positive = {'category': 'data_exposure', 'assessment': 'applies', 'excerpts': [source],
        'example_ids': ['public-exposure'], 'reason': 'Confidential data is moved to a public audience.',
        'missing_information': []}
    dismissal = {'category': 'mass_deletion', 'assessment': 'does_not_apply', 'excerpts': [source],
        'example_ids': ['public-exposure'], 'reason': 'Moving one file is not broad deletion.',
        'missing_information': []}
    invalid = {'findings': [positive, dismissal], 'coverage_complete': True,
        'unmapped_concerns': [], 'explanation': 'Data exposure applies; mass deletion does not.',
        'unknown_dispositions': []}
    corrected = deepcopy(invalid)
    corrected['findings'][1]['example_ids'] = []
    messages = []
    def factory(schema, profile, purpose):
        def invoke(prompt):
            messages.append(deepcopy(prompt))
            return deepcopy(invalid if len(messages) == 1 else corrected)
        return SimpleNamespace(invoke=invoke)
    monkeypatch.setattr(llm_routing, 'make_parser', factory)
    routing = llm_routing.current_routing().model_copy(deep=True)
    routing.purposes['semantic_judge'].max_output_attempts = 2
    monkeypatch.setattr(llm_routing, 'current_routing', lambda: routing)
    attempt, token = monitoring.begin()
    try:
        result = semantic_judge.judge(source, analysis, retrieval)
    finally:
        monitoring.end(token)
    assert result['status'] == 'applied' and result['output_attempts'] == 2
    assert result['result']['findings'][0] == positive
    assert result['result']['findings'][1]['assessment'] == 'does_not_apply'
    assert [len(m) for m in messages] == [2, 3]
    feedback = messages[1][-1]['content']
    assert 'citation_category_mismatch' in feedback
    assert 'example_ids may be []' in feedback
    assert 'Do not invent uncertainty' in feedback
    assert 'previous_response' in feedback
    trace = attempt.events[0]['parser_trace']
    assert trace['response'] == invalid and trace['correction_message'] == feedback
    semantic_judge.validate_frozen_result(result, analysis, retrieval, source=source)


def test_plain_move_has_a_valid_dismissal_without_a_benign_overwrite_example():
    from src.services import semantic_judge
    from src.services.semantic_failures import SemanticValidationError
    source, analysis, retrieval, value = source_dismissal_case('destructive_overwrite')
    source = 'Move /workspace/one.txt to /workspace/two.txt.'
    analysis['result']['actions'] = [{'action_class': 'move_file', 'excerpt': source}]
    value['findings'][0].update(excerpts=[source],
        reason='This move rejects destination collisions; it cannot overwrite an existing file.')
    semantic_judge.validate_result(value, analysis, retrieval, source=source,
        validate_action_effects=True, validate_citation_consistency=True, allow_source_dismissals=True)
    value['findings'][0].update(assessment='uncertain',
        missing_information=['Whether the destination already exists.'])
    value['coverage_complete'] = False
    with pytest.raises(SemanticValidationError) as caught:
        semantic_judge.validate_result(value, analysis, retrieval, source=source,
            validate_action_effects=True, validate_citation_consistency=True, allow_source_dismissals=True)
    assert caught.value.validation_errors['findings.0'][0]['code'] == 'unsupported_move_overwrite_finding'


@pytest.mark.parametrize('issue,field,code', [
    ('omitted', 'findings', 'required_categories_missing_or_duplicated'),
    ('duplicated', 'findings', 'required_categories_missing_or_duplicated'),
    ('uncertain_without_fact', 'findings.0.missing_information', 'missing_uncertainty_reason'),
    ('unresolved_unknown', 'coverage_complete', 'complete_with_unresolved_assessment'),
])
def test_source_dismissals_do_not_erase_coverage_requirements(issue, field, code):
    from copy import deepcopy
    from src.services import semantic_judge
    from src.services.semantic_failures import SemanticValidationError
    source, analysis, retrieval, value = source_dismissal_case()
    if issue == 'omitted':
        value['findings'] = []
    elif issue == 'duplicated':
        value['findings'].append(deepcopy(value['findings'][0]))
    elif issue == 'uncertain_without_fact':
        value['findings'][0]['assessment'] = 'uncertain'
    else:
        analysis['result']['unknowns'] = ['A material unknown.']
        value['unknown_dispositions'] = [{'unknown_index': 0, 'disposition': 'unresolved',
            'reason': 'This material fact is still needed.', 'excerpts': [], 'example_ids': []}]
    with pytest.raises(SemanticValidationError) as caught:
        semantic_judge.validate_result(value, analysis, retrieval, source=source,
            strict_consistency=True, validate_citation_consistency=True, allow_source_dismissals=True)
    assert caught.value.validation_errors[field][0]['code'] == code
