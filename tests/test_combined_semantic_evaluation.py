"""Offline checks for suite preservation, parser scoring and report coverage."""
from types import SimpleNamespace
import pytest

from src.config import settings
from src.models.request import ActionRequest
from src.services import llm_parser, semantic_evaluation as evaluation, semantic_judge
from src.services.semantic_v3_corpus import load


def test_combined_suite_preserves_all_source_records_and_ids():
    merged, expectations = evaluation.load_suite('semantic-combined-v1')
    development, _ = evaluation.load_suite('semantic-development-v1')
    assert merged.cases == load('evaluation').cases + development.cases
    assert len(merged.cases) == len(expectations) == 88
    assert {id for id, a in expectations.items() if not a.get('action_classes_required', True)} == {
        'eval-deletion-03', 'eval-bypass-03'}


def test_transport_failure_with_retained_analysis_is_not_clarification():
    _, expectations = evaluation.load_suite('semantic-combined-v1')
    analysis = {'result': {'actions': [{'action_class': 'move_file', 'excerpt':
        '/srv/agent/workspace/public-handbook.txt'}]}}
    error = llm_parser.OnlineActionParseError('provider failed')
    error.__cause__ = TimeoutError('network timeout')
    result = evaluation._compare_parser(None, analysis, expectations['fresh-052'], error)
    assert result['action_classes_match'] is True
    assert result['handling_match'] is False


def test_grounding_failure_is_scored_as_missing_input_rejection():
    _, expectations = evaluation.load_suite('semantic-combined-v1')
    analysis = {'result': {'actions': [{'action_class': 'move_file', 'excerpt':
        '/srv/agent/workspace/public-handbook.txt'}]}}
    error = llm_parser.OnlineActionParseError('parser rejected action')
    error.__cause__ = llm_parser.CorrectableParserError(
        'a single action requires a grounded executable action')
    result = evaluation._compare_parser(None, analysis, expectations['fresh-052'], error)
    assert result['handling_match'] is True
    assert result['parsed_action_details_match'] is None


def test_ambiguous_legacy_class_is_not_required():
    _, expectations = evaluation.load_suite('semantic-combined-v1')
    error = llm_parser.UnverifiedActionError('unsupported_action: ambiguous resource type')
    result = evaluation._compare_parser(None, {'result': {'actions': []}},
                                        expectations['eval-bypass-03'], error)
    assert result['action_classes_match'] is None
    assert result['handling_match'] is True


def test_split_requires_a_split_rejection_not_a_transport_failure():
    dataset, expectations = evaluation.load_suite('semantic-combined-v1')
    case = next(c for c in dataset.cases if c.id == 'eval-combined-04')
    analysis = {'result': {'actions': [
        {'action_class': a.action_class.value, 'excerpt': case.instruction} for a in case.actions]}}
    expected = expectations[case.id]
    failure = evaluation._compare_parser(None, analysis, expected, TimeoutError())
    assert failure['handling_match'] is False
    rejection = llm_parser.UnverifiedActionError('split_required: submit separately')
    result = evaluation._compare_parser(None, analysis, expected, rejection)
    assert result['handling_match'] is True
    assert result['all_targets_match'] is True


@pytest.mark.parametrize('judge_available', [True, False])
def test_combined_runner_uses_original_instruction_and_reports_source(monkeypatch, judge_available):
    monkeypatch.setattr(settings, 'OFFLINE_MODE', False)
    monkeypatch.setattr(settings, 'SEMANTIC_PIPELINE_MODE', 'baseline')
    monkeypatch.setattr(evaluation.time, 'sleep', lambda _: None)
    dataset, _ = evaluation.load_suite('semantic-combined-v1')
    case = next(c for c in dataset.cases if c.id == 'eval-bypass-02')
    inputs = []

    def parse(source):
        inputs.append(source.raw_prompt)
        request = ActionRequest(agent_id=source.agent_id, actor_role=source.actor_role,
            action_class='read_file', target_resource='/srv/agent/workspace/manual.txt',
            parameters={'raw_prompt': source.raw_prompt, 'parser_provenance': {}})
        request._semantic_analysis = {'status': 'applied', 'result': {
            'actions': [{'action_class': 'read_file', 'excerpt': source.raw_prompt}],
            'unsupported_actions': [], 'potential_risks': [], 'unmapped_concerns': []}}
        return request

    judgment = {'status': 'applied', 'result': {'findings': [
        {'category': 'review_bypass', 'assessment': 'does_not_apply'}],
        'coverage_complete': True, 'unmapped_concerns': []}}
    if not judge_available:
        judgment = {'status': 'unavailable', 'error': 'TimeoutError'}
    monkeypatch.setattr(llm_parser, 'parse', parse)
    monkeypatch.setattr(evaluation.semantic_engine, 'assess', lambda _: SimpleNamespace(
        as_evidence=lambda: {'flag': False, 'candidates': []}))
    monkeypatch.setattr(evaluation.semantic_pipeline, 'run', lambda *args, **kwargs: {
        'retrieval': {'status': 'applied', 'candidates': []},
        'judgment': judgment, 'proposed_flag': not judge_available})
    monkeypatch.setattr(semantic_judge, 'judge', lambda *args: judgment)
    report = evaluation.run(suite='semantic-combined-v1', case_id=case.id)
    assert inputs == [case.instruction]
    assert report['dataset_total'] == 88
    assert report['counts']['total'] == 1
    assert report['source_counts']['legacy-held-out']['attempted'] == 1
    assert report['counts']['parser_action_details_matches'] == 1
    if judge_available:
        assert report['cases'][0]['finding_matches'] == {'review_bypass': True}
        assert report['counts']['assessed'] == 1
    else:
        assert report['cases'][0]['status'] == 'semantic_unavailable'
        assert 'flag_match' not in report['cases'][0]
        assert report['counts']['assessed'] == 0
    assert settings.SEMANTIC_PIPELINE_MODE == 'baseline'


def test_early_stop_includes_unattempted_source_totals(monkeypatch):
    monkeypatch.setattr(settings, 'OFFLINE_MODE', False)
    monkeypatch.setattr(evaluation.time, 'sleep', lambda _: None)

    def unavailable(_):
        raise llm_parser.OnlineActionParseError('provider unavailable')

    monkeypatch.setattr(llm_parser, 'parse', unavailable)
    report = evaluation.run(suite='semantic-combined-v1')
    assert report['stopped_early'] is True
    assert report['counts']['attempted'] == 3
    assert len(report['unattempted_case_ids']) == 85
    assert report['source_counts']['legacy-held-out']['total'] == 32
    assert report['source_counts']['semantic-development-v1']['total'] == 56
    assert report['source_counts']['semantic-development-v1']['attempted'] == 0
