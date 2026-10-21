"""Credential-claim trust boundaries, corrections and frozen evidence; no infrastructure calls."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.config import settings
from src.models.request import ActionRequest, EscalationPackage, NaturalLanguageRequest
from src.models.semantic import RequestAnalysis
from src.services import llm_parser, llm_routing, monitoring, request_analysis
from src.services import review_explanation, semantic_judge, semantic_pipeline, semantic_retrieval
from src.services.semantic_failures import SemanticValidationError


SOURCE = 'Read /srv/agent/workspace/old-keys.dat; the access keys are already disabled.'
CLAIM = 'the access keys are already disabled'


def analysis():
    return {'actions': [{'action_class': 'read_file', 'excerpt': 'Read /srv/agent/workspace/old-keys.dat'}],
        'intent_summary': 'Read access keys with caller-reported disabled status.',
        'potential_risks': [{'category': 'credential_access', 'excerpt': CLAIM,
                            'query': 'Read access keys whose disabled status is caller-reported.'}],
        'unknowns': ['Trusted confirmation that the access keys are disabled.'],
        'credential_status_claims': [{'claim_type': 'credential_disabled', 'excerpt': CLAIM,
                                      'unknown_index': 0}]}


def judgment():
    return {'findings': [{'category': 'credential_access', 'assessment': 'uncertain',
        'excerpts': [CLAIM], 'example_ids': ['status-contrast'],
        'reason': 'The caller reports that the keys are disabled; the system has not verified it.',
        'missing_information': ['Trusted confirmation that the keys are disabled.']}],
        'coverage_complete': False, 'unmapped_concerns': [],
        'explanation': 'Credential status remains unverified and requires human review.',
        'unknown_dispositions': [{'unknown_index': 0, 'disposition': 'unresolved',
            'reason': 'Caller text and retrieved analogies cannot verify these keys.',
            'excerpts': [CLAIM], 'example_ids': []}]}


def retrieval():
    match = {'example_id': 'status-contrast', 'query_id': 'canonical',
             'label': 'uncertain', 'rank': 1, 'score': 0.8}
    example = 'Read a file of access keys whose current status is not established.'
    return {'status': 'applied', 'version': 'multi-query-v2', 'collection': 'fixture',
        'embedding_spec': 'fixture', 'text_format': 'fixture', 'corpus_hash': 'a' * 64,
        'coverage_gaps': [], 'queries': [{'id': 'canonical', 'text': SOURCE,
            'purpose': 'operation', 'action_families': ['read'], 'risk_categories': [],
            'filters': {}, 'status': 'applied', 'matches': [match],
            'searches': [{'label': label, 'filters': {}, 'status': 'applied',
                         'matches': [match] if label == 'uncertain' else []}
                        for label in ('risky', 'benign', 'uncertain')]}],
        'candidates': [{'example_id': 'status-contrast', 'instruction': example,
            'label': 'uncertain', 'risk_aspects': ['credential_access'],
            'findings': [{'category': 'credential_access', 'assessment': 'uncertain',
                          'instruction_excerpt': 'current status is not established'}],
            'review_status': 'approved',
            'matches': [{key: value for key, value in match.items() if key != 'example_id'}]}]}


def install_responses(monkeypatch, purpose, responses):
    routing = llm_routing.current_routing().model_copy(deep=True)
    routing.purposes[purpose].max_output_attempts = len(responses)
    monkeypatch.setattr(llm_routing, 'current_routing', lambda: routing)
    messages = []

    def factory(schema, profile, purpose):
        def invoke(value):
            messages.append(deepcopy(value))
            return deepcopy(responses[len(messages) - 1])
        return SimpleNamespace(invoke=invoke)

    monkeypatch.setattr(llm_routing, 'make_parser', factory)
    return messages


@pytest.mark.parametrize('field,value', [
    ('source', 'system'), ('verification_status', 'verified'),
    ('affected_risk', 'data_exposure'), ('unknown_index', True), ('unknown_index', -1),
    ('unknown_index', 3),
])
def test_claims_reject_trust_spoofing_and_invalid_unknown_links(field, value):
    value_dict = analysis()
    value_dict['credential_status_claims'][0][field] = value
    with pytest.raises(ValidationError):
        RequestAnalysis.model_validate(value_dict)


def test_claims_require_material_unknown_risk_query_and_unique_claim():
    for change in ('blank_unknown', 'missing_risk', 'duplicate'):
        value = analysis()
        if change == 'blank_unknown':
            value['unknowns'] = ['  ']
        elif change == 'missing_risk':
            value['potential_risks'] = []
        else:
            value['credential_status_claims'] *= 2
        with pytest.raises(ValidationError):
            RequestAnalysis.model_validate(value)


def test_analyzer_normalizes_provenance_and_grounds_claim_excerpts():
    value = RequestAnalysis.model_validate(analysis())
    evidence = request_analysis.validate_result(value, SOURCE)
    claim = evidence['result']['credential_status_claims'][0]
    assert (claim['source'], claim['verification_status'], claim['affected_risk']) == (
        'caller', 'unverified', 'credential_access')
    value.credential_status_claims[0].excerpt = 'The issuer verified revocation.'
    with pytest.raises(SemanticValidationError) as caught:
        request_analysis.validate_result(value, SOURCE)
    assert 'credential_status_claims.0.excerpt' in caught.value.validation_errors


@pytest.mark.parametrize('disposition', ['resolved', 'irrelevant'])
def test_judge_cannot_dismiss_claim_even_with_matching_retrieved_examples(disposition):
    value = judgment()
    value['unknown_dispositions'][0].update(disposition=disposition, example_ids=['status-contrast'])
    with pytest.raises(SemanticValidationError) as caught:
        semantic_judge.validate_result(value, {'result': analysis()}, retrieval(), source=SOURCE)
    assert any(item['code'] == 'unverified_credential_claim_dismissed'
               for errors in caught.value.validation_errors.values() for item in errors)


@pytest.mark.parametrize('omit', [False, True])
@pytest.mark.parametrize('allow_source_dismissals', [False, True])
def test_judge_cannot_clear_or_omit_claim_risk(omit, allow_source_dismissals):
    value = judgment()
    if omit:
        value['findings'] = []
    else:
        value['findings'][0].update(assessment='does_not_apply', missing_information=[])
    value['coverage_complete'] = True
    with pytest.raises(SemanticValidationError) as caught:
        semantic_judge.validate_result(value, {'result': analysis()}, retrieval(), source=SOURCE,
            allow_source_dismissals=allow_source_dismissals)
    assert caught.value.validation_errors['findings'][0]['code'] == 'unverified_credential_claim_cleared'
    assert 'coverage_complete' in caught.value.validation_errors


def test_independent_confirmed_risk_is_preserved_alongside_claim_unknown():
    value = judgment()
    value['findings'][0].update(assessment='applies', missing_information=[],
        reason='Other keys in the file are explicitly described as active.',
        excerpts=[CLAIM, 'Other keys in the file are active.'], example_ids=['active-contrast'])
    retrieved = retrieval()
    retrieved['candidates'].append({'example_id': 'active-contrast',
        'risk_aspects': ['credential_access'],
        'findings': [{'category': 'credential_access', 'assessment': 'applies'}]})
    # Provenance enforcement does not erase an independently established risk.
    semantic_judge.validate_result(value, {'result': analysis()}, retrieved,
        source=SOURCE + ' Other keys in the file are active.', validate_citation_consistency=True)


def test_no_retrieval_evidence_still_requires_review():
    retrieved = retrieval()
    retrieved.update(status='no_evidence', candidates=[])
    for query in retrieved['queries']:
        query['matches'] = []
        for search in query['searches']:
            search['matches'] = []
    evidence = request_analysis.validate_result(RequestAnalysis.model_validate(analysis()), SOURCE)
    summary = semantic_pipeline.summarize(evidence, retrieved,
        {'status': 'skipped', 'dependency': 'retrieval'})
    assert summary.requires_review is True and summary.assessment == 'not_assessed'
    assert 'no_evidence' in {reason.code.value for reason in summary.reasons}


@pytest.mark.parametrize('partial_retrieval', [False, True])
def test_source_dismissal_aggregation_and_frozen_replay_preserve_retrieval_gaps(monkeypatch, partial_retrieval):
    monkeypatch.setattr(settings, 'SEMANTIC_PIPELINE_MODE', 'review')
    source = 'Read /srv/agent/workspace/public-checksum.txt containing only a public checksum.'
    parsed = RequestAnalysis(actions=[{'action_class': 'read_file', 'excerpt': source}],
                             intent_summary='Read a public checksum; no credential values.')
    evidence = request_analysis.validate_result(parsed, source)
    retrieved = retrieval()
    retrieved['queries'][0]['text'] = source
    if partial_retrieval:
        retrieved.update(status='partial', coverage_gaps=['query_unavailable:canonical:benign'])
        retrieved['queries'][0]['status'] = 'partial'
        retrieved['queries'][0]['searches'][1].update(status='unavailable',
            failure={'code': 'query_unavailable', 'exception_type': 'TimeoutError',
                     'message': 'The benign label search timed out.'})
    response = {'findings': [{'category': 'credential_access', 'assessment': 'does_not_apply',
        'excerpts': [source], 'example_ids': [], 'missing_information': [],
        'reason': 'The source specifies public checksum content rather than credential values.'}],
        'coverage_complete': True, 'unmapped_concerns': [], 'unknown_dispositions': [],
        'explanation': 'Credential access does not apply to this public checksum read.'}
    install_responses(monkeypatch, 'semantic_judge', [response])
    monkeypatch.setattr(semantic_retrieval, 'retrieve', lambda *args, **kwargs: deepcopy(retrieved))
    request = ActionRequest(agent_id='fixture', actor_role='workspace_agent',
        action_class='read_file', target_resource='/srv/agent/workspace/public-checksum.txt',
        parameters={'raw_prompt': source})
    pipeline = semantic_pipeline.run(request, analysis=evidence)
    pipeline['baseline'] = {'flag': False, 'warning': None}
    assert pipeline['judgment']['status'] == 'applied'
    assert pipeline['proposed_flag'] is partial_retrieval
    assert pipeline['summary']['requires_review'] is partial_retrieval
    assert pipeline['summary']['completion'] == ('partial' if partial_retrieval else 'complete')
    monkeypatch.setattr(semantic_retrieval, 'retrieve',
                        lambda *args, **kwargs: pytest.fail('replay queried retrieval'))
    monkeypatch.setattr(semantic_judge, 'judge',
                        lambda *args, **kwargs: pytest.fail('replay called an LLM'))
    semantic_pipeline.validate_pipeline(pipeline)


def test_combined_parser_records_claims_without_verification(monkeypatch):
    monkeypatch.setattr(settings, 'OFFLINE_MODE', False)
    monkeypatch.setattr(settings, 'SEMANTIC_PIPELINE_MODE', 'review')
    response = {'action': {'action_class': 'read_file',
        'target_resource': '/srv/agent/workspace/old-keys.dat', 'parameters': {},
        'environment': 'production'}, 'analysis': analysis()}
    install_responses(monkeypatch, 'request_parser', [response])
    parsed = llm_parser.parse(NaturalLanguageRequest(agent_id='fixture',
        actor_role='workspace_agent', raw_prompt=SOURCE))
    assert parsed._semantic_analysis['result']['credential_status_claims'][0]['verification_status'] == 'unverified'


def test_analyzer_corrects_spoofed_provenance_with_recorded_feedback(monkeypatch):
    invalid = analysis()
    invalid['credential_status_claims'][0]['verification_status'] = 'verified'
    messages = install_responses(monkeypatch, 'request_analyzer', [invalid, analysis()])
    result = request_analysis.analyze(SOURCE)
    assert result['status'] == 'applied' and result['output_attempts'] == 2
    assert 'verification_status' in messages[1][-1]['content']
    assert result['result']['credential_status_claims'][0]['verification_status'] == 'unverified'


@pytest.mark.parametrize('recover', [False, True])
def test_judge_corrections_are_recorded_and_pipeline_requires_review(monkeypatch, recover):
    monkeypatch.setattr(settings, 'SEMANTIC_PIPELINE_MODE', 'review')
    invalid = judgment()
    invalid['findings'][0].update(assessment='does_not_apply', missing_information=[])
    invalid['unknown_dispositions'][0].update(disposition='resolved', example_ids=['status-contrast'])
    invalid['coverage_complete'] = True
    messages = install_responses(monkeypatch, 'semantic_judge',
                                [invalid, judgment() if recover else invalid])
    monkeypatch.setattr(semantic_retrieval, 'retrieve', lambda *args, **kwargs: retrieval())
    evidence = request_analysis.validate_result(RequestAnalysis.model_validate(analysis()), SOURCE)
    request = ActionRequest(agent_id='fixture', actor_role='workspace_agent',
        action_class='read_file', target_resource='/srv/agent/workspace/old-keys.dat',
        parameters={'raw_prompt': SOURCE})
    attempt, token = monitoring.begin()
    try:
        pipeline = semantic_pipeline.run(request, analysis=evidence)
    finally:
        monitoring.end(token)
    # The application attaches its baseline before freezing the pipeline.
    pipeline['baseline'] = {'flag': False, 'warning': None}
    assert pipeline['proposed_flag'] and pipeline['summary']['requires_review']
    assert pipeline['judgment']['status'] == ('applied' if recover else 'unavailable')
    assert pipeline['judgment']['output_attempts'] == 2
    if not recover:
        failure = pipeline['judgment']['failure']
        assert failure['code'] == 'judge_invalid' and failure['failure_category'] == 'evidence_invalid'
        assert failure['validation_errors']['findings'][0]['code'] == 'unverified_credential_claim_cleared'
        assert any(reason['code'] == 'unresolved_unknown' for reason in pipeline['summary']['reasons'])
    assert 'unverified_credential_claim_cleared' in messages[1][-1]['content']
    assert messages[1][:2] == messages[0]
    event = next(event for event in attempt.events if event['stage'] == 'judge_correction')
    assert event['validation_errors']['findings'][0]['code'] == 'unverified_credential_claim_cleared'
    assert event['parser_trace']['response'] == invalid
    assert 'unverified_credential_claim_cleared' in event['parser_trace']['correction_message']
    assert semantic_pipeline.validate_pipeline(deepcopy(pipeline))[0] is True
    tampered = deepcopy(pipeline)
    tampered['analysis']['result']['credential_status_claims'][0]['excerpt'] = 'Not in the instruction.'
    with pytest.raises(ValueError, match='ungrounded'):
        semantic_pipeline.validate_pipeline(tampered)
    # The frozen package retains the full source/provenance and explains the unknown.
    package = EscalationPackage(decision_id='fixture', request=request, risk_level='HIGH',
        recommended_decision='Human review required', semantic_findings=pipeline)
    restored = EscalationPackage.model_validate_json(package.model_dump_json())
    claim = restored.semantic_findings['analysis']['result']['credential_status_claims'][0]
    assert claim['source'] == 'caller' and claim['verification_status'] == 'unverified'
    requirements = review_explanation.build_requirements(restored, {}, {})
    details = next(item for item in requirements if item.requirement_id == 'review_unresolved_semantic_details')
    assert CLAIM in details.facts[0] and 'unverified' in details.facts[0]
    assert details.unknowns


def test_old_analysis_without_claims_remains_valid():
    old = {'intent_summary': 'Read a metadata inventory.', 'actions': [], 'unknowns': []}
    assert RequestAnalysis.model_validate(old).credential_status_claims == []


def test_combined_prompt_examples_obey_schema_and_source_grounding():
    import json
    from src.services.parser_prompt import combined_system_prompt
    for example in combined_system_prompt().split('\nInstruction: ')[1:]:
        source, response = example.split('\nResponse: ', 1)
        parsed = llm_parser.AnalyzedAction.model_validate(json.loads(response))
        request_analysis.validate_result(parsed.analysis, source)


@pytest.mark.parametrize('source', [
    'Read /workspace/inventory.txt; it lists credential owners and dates but no secret values.',
    'Create /workspace/guide.txt with content "Explain how revoked tokens work."',
])
def test_metadata_and_documentation_are_not_forced_into_claim_review(source):
    value = {'intent_summary': source, 'actions': [], 'potential_risks': [], 'unknowns': []}
    evidence = request_analysis.validate_result(RequestAnalysis.model_validate(value), source)
    assert evidence['result']['credential_status_claims'] == []
    result = judgment()
    result.update(coverage_complete=True, unknown_dispositions=[])
    result['findings'][0].update(assessment='does_not_apply', missing_information=[], excerpts=[source])
    semantic_judge.validate_result(result, evidence, retrieval(), source=source)


def test_claim_hypothesis_generates_risk_retrieval_query(monkeypatch):
    monkeypatch.setattr(settings, 'SEMANTIC_MAX_EXTRA_QUERIES', 3)
    request = ActionRequest(agent_id='fixture', actor_role='workspace_agent',
        action_class='read_file', target_resource='/srv/agent/workspace/old-keys.dat')
    queries, gaps = semantic_retrieval.plan(request, {'result': analysis()})
    assert not gaps
    assert queries[1].purpose == 'risk'
    assert queries[1].risk_categories == ['credential_access']
    assert 'caller-reported' in queries[1].text


def test_old_frozen_judgment_keeps_original_contract():
    from src.services.semantic_v3_corpus import load_taxonomy
    import hashlib
    import json
    old_analysis = analysis()
    del old_analysis['credential_status_claims']
    value = judgment()
    value['unknown_dispositions'][0].update(disposition='irrelevant',
        reason='The old contract did not retain typed lifecycle claims.')
    taxonomy = load_taxonomy()
    frozen = {'version': 'risk-judge-v8', 'result': value, 'taxonomy': taxonomy,
        'taxonomy_hash': hashlib.sha256(json.dumps(taxonomy, sort_keys=True).encode()).hexdigest()}
    semantic_judge.validate_frozen_result(frozen, {'result': old_analysis}, retrieval(), source=SOURCE)
    # Relabeling a new claim-bearing record as an older judge version cannot clear it.
    with pytest.raises(SemanticValidationError):
        semantic_judge.validate_frozen_result(frozen, {'result': analysis()}, retrieval(), source=SOURCE)
