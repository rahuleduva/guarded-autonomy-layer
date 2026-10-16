"""Purpose routes choose the configured adapter/model and keep fallback bounded."""
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.models.semantic import ActionMention, RequestAnalysis, JudgeAssessment
from src.services import llm_routing, monitoring, request_analysis


class ProviderError(Exception):
    def __init__(self, status_code: int):
        self.status_code = status_code


SOURCE = 'Original instruction:\nRead /srv/agent/workspace/notes.txt.'


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
