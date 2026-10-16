"""Non-executing online evaluation; held-out answers never enter model inputs."""
from datetime import datetime, timezone
import json
import time
from src.config import PROJECT_ROOT, settings
from src.models.request import NaturalLanguageRequest
from src.services import llm_parser, request_analysis, semantic_engine, semantic_pipeline
from src.services.semantic_v3_corpus import Dataset, load


def _safe_error_details(exc):
    """Capture exception types/statuses without provider messages or credentials."""
    chain=[]
    current=exc
    seen=set()
    while current is not None and id(current) not in seen and len(chain)<8:
        seen.add(id(current))
        item={'type':type(current).__name__}
        status=getattr(current,'status_code',None)
        if isinstance(status,int):
            item['http_status']=status
        chain.append(item)
        current=current.__cause__ or current.__context__
    return chain


DEVELOPMENT_CASES = PROJECT_ROOT / 'examples/semantic_evaluation_v1/cases.json'
SUITE_DIRECTORIES = {
    'semantic-development-v1': DEVELOPMENT_CASES.parent,
    'semantic-combined-v1': PROJECT_ROOT / 'examples/semantic_evaluation_combined_v1',
}
OPERATION_PARAMETER_KEYS = {'raw_prompt', 'parser_provenance'}


def load_suite(suite):
    """Load answer files locally before any online calls."""
    if suite not in SUITE_DIRECTORIES:
        raise ValueError(f'unknown semantic evaluation suite: {suite}')
    directory = SUITE_DIRECTORIES[suite]
    dataset = Dataset.model_validate_json((directory / 'cases.json').read_text())
    if dataset.split != 'held_out':
        raise ValueError('evaluation suite must not be eligible for retrieval seeding')
    sidecar = json.loads((directory / 'parser_expectations.json').read_text())
    if sidecar.get('version') != 'parser-expectations-v1':
        raise ValueError('unsupported parser expectation version')
    expectations = sidecar['cases']
    if set(expectations) != {case.id for case in dataset.cases}:
        raise ValueError('parser expectation IDs differ from dataset')
    for case in dataset.cases:
        expected = expectations[case.id]
        if [a['action_class'] for a in expected['expected_actions']] != [a.action_class.value for a in case.actions]:
            raise ValueError(f'parser action annotations disagree: {case.id}')
        disposition = expected['parser_disposition']
        if disposition not in {'parsed', 'split_required', 'clarification_or_rejection'}:
            raise ValueError(f'unknown parser disposition: {case.id}')
        if (disposition == 'split_required') != (len(case.actions) > 1):
            raise ValueError(f'parser split annotation disagrees: {case.id}')
        if expected.get('action_classes_required', True) is not True and disposition != 'clarification_or_rejection':
            raise ValueError('only ambiguous rejected requests may omit strict action-class scoring')
        if expected['expected_environment'] not in {'production', 'development', 'staging'}:
            raise ValueError(f'unsupported expected environment: {case.id}')
    return dataset, expectations


def _grounding_rejection(exc):
    """A provider failure or malformed output is not successful abstention."""
    current, seen = exc, set()
    for _ in range(8):
        if current is None or id(current) in seen:
            return False
        seen.add(id(current))
        if isinstance(current, llm_parser.ClarificationRequired):
            return True
        if isinstance(current, llm_parser.UnverifiedActionError):
            return (str(current).startswith('unsupported_action:') or str(current) in {
                'explicit file content is required',
                'move destination was not specified in the request',
                'target path was not specified in the request',
                'empty file content must be explicitly requested',
            })
        if isinstance(current, llm_parser.CorrectableParserError):
            return str(current) == 'a single action requires a grounded executable action'
        # Follow explicit wrappers, not unrelated error-handling contexts.
        current = current.__cause__
    return False


def _compare_parser(request, analysis, expected, parse_exception=None):
    """Compare actual output to sidecar answers, which are never model input."""
    expected_actions = expected['expected_actions']
    analyzed_actions = analysis.get('result', {}).get('actions', [])
    action_classes = [a.get('action_class') for a in analyzed_actions]
    expected_classes = [a['action_class'] for a in expected_actions]
    classes_match = (action_classes == expected_classes
                     if expected.get('action_classes_required', True) else None)
    if isinstance(parse_exception, llm_parser.ClarificationRequired) and not analyzed_actions:
        classes_match = None
    disposition = expected['parser_disposition']
    if disposition == 'split_required':
        handling_match = (len(action_classes) > 1 and request is None
                          and isinstance(parse_exception, llm_parser.UnverifiedActionError)
                          and str(parse_exception).startswith('split_required:'))
        target_matches = [item['target_resource'] in (analyzed_actions[index].get('excerpt') or '')
                          for index, item in enumerate(expected_actions[:len(analyzed_actions)])]
        detail_matches = None
    elif disposition == 'clarification_or_rejection':
        handling_match = request is None and _grounding_rejection(parse_exception)
        target_matches = [item['target_resource'] in (analyzed_actions[i].get('excerpt') or '')
                          for i, item in enumerate(expected_actions[:len(analyzed_actions)])]
        detail_matches = None
    else:
        handling_match = request is not None
        target_matches = ([request.target_resource == expected_actions[0]['target_resource']]
                          if request is not None and len(expected_actions) == 1 else [])
        if request is None or len(expected_actions) != 1:
            detail_matches = None
        else:
            action = expected_actions[0]
            actual_parameters = {k:v for k,v in request.parameters.items()
                                 if k not in OPERATION_PARAMETER_KEYS}
            detail_matches = {
                'action_class': request.action_class == action['action_class'],
                'target_resource': request.target_resource == action['target_resource'],
                'parameters': actual_parameters == action['parameters'],
                'environment': request.environment == expected['expected_environment'],
            }
    return {
        'expected_disposition': disposition,
        'actual_executable_action_returned': request is not None,
        'action_classes_match': classes_match,
        'handling_match': bool(handling_match),
        'target_matches': target_matches,
        'all_targets_match': len(target_matches) == len(expected_actions) and all(target_matches),
        'parsed_action_details': detail_matches,
        'parsed_action_details_match': all(detail_matches.values()) if detail_matches is not None else None,
    }


def _counts(rows, *, total=None):
    return dict(total=len(rows) if total is None else total, attempted=len(rows),
        statuses={s:sum(r['status']==s for r in rows) for s in sorted({r['status'] for r in rows})},
        action_classes_assessed=sum(r['action_match'] is not None for r in rows),
        action_matches=sum(r['action_match'] is True for r in rows),
        handling_matches=sum(r.get('handling_match',False) for r in rows),
        parser_disposition_matches=sum(r.get('parser_expectations',{}).get('handling_match',False) for r in rows),
        parser_action_details_assessed=sum(r.get('parser_expectations',{}).get('parsed_action_details_match') is not None for r in rows),
        parser_action_details_matches=sum(r.get('parser_expectations',{}).get('parsed_action_details_match') is True for r in rows),
        assessed=sum(r['status']=='assessed' for r in rows),
        flag_matches=sum(r.get('flag_match') is True for r in rows))


def run(*, held_out=False, case_id=None, suite=None, max_consecutive_analysis_failures=3):
    if settings.OFFLINE_MODE:
        raise ValueError('semantic evaluation requires online providers')
    if suite and held_out:
        raise ValueError('choose either --suite or --held-out')
    expectations = None
    if suite is not None:
        dataset, expectations = load_suite(suite)
    else:
        dataset=load('evaluation' if held_out else 'corpus')
    rows=[]
    selected=dataset.cases
    if case_id is not None:
        selected=[case for case in dataset.cases if case.id==case_id]
        if not selected:
            raise ValueError(f'case ID not found in selected dataset: {case_id}')
    source_by_id = {
        case.id: expectations[case.id].get('source_suite', suite) if expectations is not None
        else ('legacy-held-out' if held_out else 'legacy-corpus')
        for case in selected
    }
    consecutive_analysis_failures=0
    stopped_early=False
    for case in selected:
        started=time.monotonic()
        # Only the instruction is sent. No risk labels, reasons or expected actions.
        previous=settings.SEMANTIC_PIPELINE_MODE
        request, parse_error, parse_error_detail, parse_exception = None, None, None, None
        try:
            settings.SEMANTIC_PIPELINE_MODE='observe'
            request=llm_parser.parse(NaturalLanguageRequest(agent_id='semantic-evaluation',
                actor_role='workspace_agent',raw_prompt=case.instruction))
            analysis=request._semantic_analysis
        except Exception as exc:
            parse_exception=exc
            analysis=getattr(exc,'analysis',None) or {'status':'unavailable','error':type(exc).__name__}
            parse_error=type(exc).__name__
            error_details=_safe_error_details(exc)
            if isinstance(exc, (ValueError, RuntimeError)):
                parse_error_detail=str(exc)[:300]
        finally:
            settings.SEMANTIC_PIPELINE_MODE=previous
        row=dict(id=case.id, expected_actions=[a.action_class.value for a in case.actions],
                 expected_label=case.label, expected_handling=case.expected_request_handling,
                 analysis=analysis, arms={})
        actual_actions=[a['action_class'] for a in analysis.get('result',{}).get('actions',[])]
        row['action_match']=actual_actions==row['expected_actions']
        row['handling_match']=(('split_required' if len(actual_actions)>1 else 'single_action')==case.expected_request_handling) if actual_actions else False
        if expectations is not None:
            row['source_suite'] = expectations[case.id].get('source_suite', suite)
            row['parser_expectations'] = _compare_parser(request, analysis, expectations[case.id], parse_exception)
            if not expectations[case.id].get('action_classes_required', True):
                row['action_match'] = None
                row['action_annotation_note'] = expectations[case.id]['compatibility_note']
            row['parser_expectations']['expected_actions'] = expectations[case.id]['expected_actions']
            row['parser_expectations']['expected_environment'] = expectations[case.id]['expected_environment']
            row['expected_risk_findings'] = [r.model_dump(mode='json') for r in case.risk_aspects]
        else:
            row['source_suite'] = 'legacy-held-out' if held_out else 'legacy-corpus'
        if isinstance(parse_exception, llm_parser.ClarificationRequired):
            consecutive_analysis_failures = 0
            row['status'] = 'needs_clarification'
            row['questions'] = parse_exception.questions
            row['validation_errors'] = parse_exception.validation_errors
            row['action_match'] = None if not actual_actions else row['action_match']
            row['handling_match'] = (row.get('parser_expectations', {}).get('handling_match', False))
        elif analysis['status']!='applied':
            row['status']='analysis_unavailable'
            row['error_chain']=error_details if parse_error else []
            consecutive_analysis_failures += 1
        elif analysis['result'].get('unsupported_actions') or not actual_actions:
            consecutive_analysis_failures=0
            row['status']='unsupported_action'
        elif len(actual_actions)!=1:
            consecutive_analysis_failures=0
            row['status']='split_required'
            row['handling_match']=case.expected_request_handling=='split_required'
        elif request is None:
            consecutive_analysis_failures=0
            row['status']='parse_unavailable'
            row['error']=parse_error
            if parse_error_detail:
                row['parse_error_detail']=parse_error_detail
        else:
            consecutive_analysis_failures=0
            try:
                row['request']=request.model_dump(mode='json')
                baseline=semantic_engine.assess(request).as_evidence()
                row['arms']['baseline']=baseline
                if actual_actions[0]!=request.action_class:
                    raise ValueError('analyzer and executable parser disagree')
                combined=semantic_pipeline.run(request,analysis=analysis)
                row['arms']['both']=combined
                # This diagnostic arm intentionally has no decision threshold.
                row['arms']['expanded_retrieval_only']=combined['retrieval']
                from src.services import semantic_judge
                canonical=dict(baseline, candidates=[dict(example_id=c.get('example_id') or c['reference'],
                    instruction=c.get('text',''), label=c['label'],
                    risk_aspects=[] if c['risk_category']=='none' else [c['risk_category']],
                    score=c['score']) for c in baseline['candidates']], coverage_gaps=[])
                judgment=semantic_judge.judge(request_analysis.source_text(request),analysis,canonical)
                flag,warning=semantic_pipeline.aggregate(analysis,canonical,judgment)
                row['arms']['judge_original_retrieval']=dict(retrieval=canonical,judgment=judgment,
                    proposed_flag=flag,proposed_warning=warning)
                row['status']='assessed'
                expected={r.category.value for r in case.risk_aspects if r.assessment=='applies'}
                actual={r['category'] for r in combined.get('judgment',{}).get('result',{}).get('findings',[]) if r['assessment']=='applies'}
                row['missed_risks']=sorted(expected-actual)
                row['additional_risks']=sorted(actual-expected)
                row['expected_findings']=[r.model_dump(mode='json') for r in case.risk_aspects]
                got={r['category']:r['assessment'] for r in combined.get('judgment',{}).get('result',{}).get('findings',[])}
                row['finding_matches']={r.category.value:got.get(r.category.value)==r.assessment for r in case.risk_aspects}
                row['flag_match']=combined['proposed_flag']==case.expected_semantic_flag
                row['retrieval_coverage']={risk:any(risk in c['risk_aspects'] and any(f['category']==risk and f['assessment']=='applies' for f in c['findings'])
                    for c in combined['retrieval'].get('candidates',[])) for risk in expected}
                if combined.get('judgment', {}).get('status') != 'applied':
                    row['status'] = 'semantic_unavailable'
                    for field in ('missed_risks', 'additional_risks', 'finding_matches', 'flag_match'):
                        row.pop(field, None)
            except Exception as exc:
                row['status']='parse_or_evaluation_unavailable'
                row['error']=type(exc).__name__
        row['elapsed_seconds']=round(time.monotonic()-started,3)
        rows.append(row)
        if consecutive_analysis_failures >= max_consecutive_analysis_failures:
            stopped_early=True
            break
        # Sequential requests; no bursts or repeated retry loops.
        time.sleep(.25)
    assessed=[r for r in rows if r['status']=='assessed']
    category_counts={}
    for row in assessed:
        for expected in row.get('expected_findings',[]):
            category=expected['category']; status=expected['assessment']
            counts=category_counts.setdefault(category,{'applies_total':0,'applies_matched':0,
                'does_not_apply_total':0,'does_not_apply_matched':0,'uncertain_total':0,'uncertain_matched':0})
            counts[status+'_total']+=1
            counts[status+'_matched']+=int(row['finding_matches'][category])
    arm_counts={}
    for name in ('baseline','judge_original_retrieval','both'):
        counts={'assessed':0,'risky_not_flagged':0,'benign_flagged':0,'uncertain_not_flagged':0,'unavailable':0}
        for row in assessed:
            arm=row['arms'].get(name,{})
            flag=arm.get('flag') if name=='baseline' else arm.get('proposed_flag')
            if flag is None: counts['unavailable']+=1; continue
            counts['assessed']+=1
            if row['expected_label']=='risky' and not flag: counts['risky_not_flagged']+=1
            if row['expected_label']=='benign' and flag: counts['benign_flagged']+=1
            if row['expected_label']=='uncertain' and not flag: counts['uncertain_not_flagged']+=1
        arm_counts[name]=counts
    attempted_ids={row['id'] for row in rows}
    unattempted_ids=[case.id for case in selected if case.id not in attempted_ids]
    return dict(version='semantic-evaluation-v2',suite=suite or ('legacy-held-out' if held_out else 'legacy-corpus'),
        created_at=datetime.now(timezone.utc).isoformat(),
        dataset_hash=dataset.fingerprint,split=dataset.split,labels_reviewed=all(c.review_status=='approved' for c in dataset.cases),
        answers_supplied_to_models=False,
        limitation='Small synthetic diagnostic. Parser failures are reported, never counted as clean semantic results. No execution performed.',
        stopped_early=stopped_early,unattempted_case_ids=unattempted_ids,
        risk_category_counts=category_counts, arm_counts=arm_counts,
        dataset_total=len(dataset.cases),
        source_counts={source:_counts([row for row in rows if row['source_suite']==source],
                                     total=sum(value==source for value in source_by_id.values()))
                       for source in sorted(set(source_by_id.values()))},
        counts=_counts(rows, total=len(selected)), cases=rows)
