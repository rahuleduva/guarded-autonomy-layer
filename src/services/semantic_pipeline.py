"""Versioned semantic evidence and deterministic aggregation; no permission grants."""
import json
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Any, Literal
from src.config import settings
from src.models.semantic import (ActionClass, RequestAnalysis, SemanticFailure,
                                 SemanticSummary, SemanticReason, RetrievedCandidate, QueryOutcome)
from src.services import monitoring, request_analysis, semantic_judge, semantic_retrieval
from src.services.semantic_failures import failure, handled

VERSION = 'semantic-pipeline-v2'
AGGREGATION_VERSION = 'risk-union-v2'
LEGACY_PAIR = ('semantic-pipeline-v1', 'risk-union-v1')


def aggregate_v1(analysis, retrieval, judgment):
    """Pure: risk/uncertainty/coverage union, never a permission grant."""
    if analysis.get('status') != 'applied':
        return True, 'request risk analysis unavailable; human review required'
    if retrieval.get('status') == 'unavailable' or not retrieval.get('candidates'):
        return True, 'semantic evidence unavailable; human review required'
    if judgment.get('status') != 'applied':
        return True, 'risk judgment unavailable; human review required'
    findings = judgment['result']['findings']
    risks = sorted({r['category'] for r in findings if r['assessment']=='applies'})
    uncertain = sorted({r['category'] for r in findings if r['assessment']=='uncertain'})
    incomplete = (retrieval.get('coverage_gaps') or not judgment['result']['coverage_complete']
        or analysis['result'].get('unmapped_concerns')
        or judgment['result'].get('unmapped_concerns'))
    reasons=[]
    if risks: reasons.append('applicable risks: '+', '.join(risks))
    if uncertain: reasons.append('uncertain risks: '+', '.join(uncertain))
    if incomplete: reasons.append('incomplete semantic coverage')
    return bool(reasons), ('; '.join(reasons)+'; human review required') if reasons else None


class StageEnvelope(BaseModel):
    model_config = ConfigDict(extra='allow')
    status: Literal['applied', 'partial', 'unavailable', 'no_evidence', 'skipped']
    failure: SemanticFailure | None = None
    result: dict[str, Any] | None = None
    dependency: str | None = None


class RetrievalEnvelope(StageEnvelope):
    """Retrieval provenance, required whenever a search completed."""

    version: Literal['multi-query-v2'] | None = None
    collection: str | None = Field(default=None, min_length=1)
    corpus_hash: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    embedding_spec: str | None = Field(default=None, min_length=1)
    text_format: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def require_completed_search_provenance(self):
        if self.status in {'applied', 'partial', 'no_evidence'}:
            required = ('version', 'collection', 'corpus_hash', 'embedding_spec', 'text_format')
            missing = [name for name in required if getattr(self, name) is None]
            if missing:
                raise ValueError('completed retrieval is missing provenance: ' + ', '.join(missing))
        return self


class PipelineEnvelope(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: Literal['semantic-pipeline-v2']
    aggregation_version: Literal['risk-union-v2']
    source_text: str = Field(min_length=1)
    analysis: StageEnvelope
    retrieval: RetrievalEnvelope
    judgment: StageEnvelope
    summary: SemanticSummary
    mode: Literal['observe', 'review']
    baseline: dict[str, Any]
    handling: Literal['single_action'] | None = None
    proposed_flag: bool = Field(strict=True)
    proposed_warning: str | None


def validate_stages(analysis, retrieval, judgment):
    for stage in (analysis, retrieval, judgment):
        parsed = StageEnvelope.model_validate(stage)
        if parsed.status == 'skipped' and not parsed.dependency:
            raise ValueError('skipped semantic stage requires a dependency')
        if parsed.status in {'unavailable', 'skipped'} and parsed.result is not None:
            raise ValueError('unsuccessful stage cannot claim a validated result')
        if parsed.status == 'unavailable' and parsed.failure is None and not stage.get('queries'):
            raise ValueError('unavailable semantic stage requires failure details')
    RetrievalEnvelope.model_validate(retrieval)
    if analysis['status'] not in {'applied', 'unavailable'}:
        raise ValueError('invalid analysis status')
    if analysis['status'] == 'applied':
        RequestAnalysis.model_validate(analysis.get('result'))
    if retrieval['status'] not in {'applied', 'partial', 'unavailable', 'no_evidence', 'skipped'}:
        raise ValueError('invalid retrieval status')
    if judgment['status'] not in {'applied', 'unavailable', 'skipped'}:
        raise ValueError('invalid judgment status')
    candidates = [RetrievedCandidate.model_validate(c) for c in retrieval.get('candidates', [])]
    queries = [QueryOutcome.model_validate(q) for q in retrieval.get('queries', [])]
    if len({c.example_id for c in candidates}) != len(candidates):
        raise ValueError('duplicate retrieval candidate')
    if len({q.id for q in queries}) != len(queries):
        raise ValueError('duplicate retrieval query')
    recorded_matches = []
    for query in queries:
        if query.failure is not None:
            if query.status != 'unavailable' or query.searches or query.matches:
                raise ValueError('failed query embedding cannot claim completed searches')
        else:
            if [search.label for search in query.searches] != ['risky', 'benign', 'uncertain']:
                raise ValueError('retrieval must record all three label search outcomes')
            states = [search.status for search in query.searches]
            expected = ('applied' if all(state == 'applied' for state in states)
                        else 'unavailable' if all(state == 'unavailable' for state in states) else 'partial')
            if query.status != expected:
                raise ValueError('query status contradicts label outcomes')
            flattened = [m.model_dump(mode='json') for search in query.searches for m in search.matches]
            if flattened != [m.model_dump(mode='json') for m in query.matches]:
                raise ValueError('query matches contradict label outcomes')
            for search in query.searches:
                if search.status == 'unavailable' and (search.failure is None or search.matches):
                    raise ValueError('unavailable label search requires failure without matches')
                if (search.status == 'partial') != bool(search.invalid_evidence):
                    raise ValueError('label integrity failures require partial status')
                if any(m.label != search.label for m in search.matches):
                    raise ValueError('label search contains incorrectly labeled matches')
        gaps = retrieval.get('coverage_gaps', [])
        if query.failure is not None and 'query_unavailable:' + query.id not in gaps:
            raise ValueError('failed query requires a recorded coverage gap')
        for search in query.searches:
            if search.status == 'unavailable' and 'query_unavailable:' + query.id + ':' + search.label not in gaps:
                raise ValueError('failed label search requires a recorded coverage gap')
            if search.invalid_evidence and 'evidence_invalid:' + query.id + ':' + search.label not in gaps:
                raise ValueError('invalid candidates require a recorded coverage gap')
        if any(m.query_id != query.id for m in query.matches):
            raise ValueError('retrieval match references a different query')
        recorded_matches.extend(m.model_dump(mode='json') for m in query.matches)
    for candidate in candidates:
        for match in candidate.matches:
            row = dict(example_id=candidate.example_id, **match.model_dump(mode='json'))
            if row not in recorded_matches or match.label != candidate.label:
                raise ValueError('candidate provenance does not match recorded retrieval')
    all_ids = {match['example_id'] for match in recorded_matches}
    if bool(all_ids - {c.example_id for c in candidates}) != ('candidate_budget_truncation' in retrieval.get('coverage_gaps', [])):
        raise ValueError('omitted candidates require a recorded candidate budget gap')
    status = retrieval['status']
    if status in {'applied', 'partial', 'no_evidence'}:
        if not queries:
            raise ValueError('completed retrieval requires query evidence')
        states = [q.status for q in queries]
        expected = ('unavailable' if all(state == 'unavailable' for state in states)
                    else 'partial' if any(state != 'applied' for state in states)
                    else 'applied' if candidates else 'no_evidence')
        if status != expected:
            raise ValueError('retrieval status contradicts query outcomes')
    if status in {'no_evidence', 'unavailable', 'skipped'} and candidates:
        raise ValueError('retrieval status contradicts available candidates')
    if status == 'unavailable' and queries and any(q.status != 'unavailable' for q in queries):
        raise ValueError('unavailable retrieval contains completed searches')
    if analysis['status'] != 'applied' and status != 'skipped':
        raise ValueError('retrieval requires completed analysis')
    if analysis['status'] == 'applied' and status == 'skipped':
        raise ValueError('valid analysis must attempt retrieval')
    if judgment['status'] == 'applied':
        if analysis['status'] != 'applied' or not candidates:
            raise ValueError('judgment requires valid upstream evidence')
        semantic_judge.validate_frozen_result(judgment, analysis, retrieval, source=None)
    if not candidates and judgment['status'] != 'skipped':
        raise ValueError('judgment must be skipped without candidates')
    if candidates and judgment['status'] == 'skipped':
        raise ValueError('valid candidates require attempted judgment')


def summarize(analysis, retrieval, judgment):
    """Collect all gaps and findings, retaining risk even alongside uncertainty."""
    validate_stages(analysis, retrieval, judgment)
    reasons = []
    def add(code, stage, message, query_id=None, risk_category=None, failure_category=None):
        reasons.append(SemanticReason(code=code, stage=stage, message=message,
                                      query_id=query_id, risk_category=risk_category,
                                      failure_category=failure_category))
    a = analysis.get('result') or {}
    j = judgment.get('result') or {}
    if analysis['status'] != 'applied':
        code = 'analysis_invalid' if (analysis.get('failure') or {}).get('code') == 'analysis_invalid' else 'analysis_unavailable'
        add(code, 'analysis', 'Request risk analysis could not be completed; downstream checks were skipped.',
            failure_category=(analysis.get('failure') or {}).get('failure_category'))
    if retrieval['status'] == 'unavailable':
        add('retrieval_unavailable', 'retrieval', 'Semantic evidence retrieval could not complete.',
            failure_category=(retrieval.get('failure') or {}).get('failure_category'))
    if retrieval['status'] == 'partial':
        add('incomplete_coverage', 'retrieval', 'Some searches or candidate integrity checks could not complete.')
    if retrieval['status'] == 'no_evidence':
        query_ids = ', '.join(q['id'] for q in retrieval.get('queries', []))
        add('no_evidence', 'retrieval', 'Semantic searches completed but found no eligible examples to support a conclusion. '
            + 'Attempted queries: ' + query_ids + '; their filters are recorded in retrieval.queries.')
    for gap in retrieval.get('coverage_gaps', []):
        prefix, _, detail = gap.partition(':')
        if prefix == 'query_unavailable':
            add('query_unavailable', 'retrieval', 'Search could not complete: ' + detail, detail.split(':')[0])
        elif prefix == 'query_budget':
            add('query_budget', 'retrieval', 'Query budget omitted risk evidence: ' + detail)
        elif prefix == 'candidate_budget_truncation':
            add('candidate_budget', 'retrieval', 'Candidate budget omitted retrieved evidence.')
        elif prefix == 'evidence_invalid':
            add('evidence_invalid', 'retrieval', 'Invalid evidence was excluded: ' + detail, detail.split(':')[0])
        elif prefix not in {'empty', 'no_candidates', 'unmapped_concerns'}:
            add('incomplete_coverage', 'retrieval', 'Retrieval coverage is incomplete: ' + gap)
    if judgment['status'] == 'unavailable':
        code = 'judge_invalid' if (judgment.get('failure') or {}).get('code') == 'judge_invalid' else 'judge_unavailable'
        add(code, 'judgment', 'Risk judgment could not be completed after the configured attempts.',
            failure_category=(judgment.get('failure') or {}).get('failure_category'))
    risks, uncertain = [], []
    if judgment['status'] == 'applied':
        for finding in j['findings']:
            if finding['assessment'] == 'applies':
                risks.append(finding['category'])
            elif finding['assessment'] == 'uncertain':
                uncertain.append(finding['category'])
                add('uncertain_finding', 'judgment', finding['reason'], risk_category=finding['category'])
        if not j['coverage_complete']:
            add('incomplete_coverage', 'judgment', 'The judge could not assess every required risk completely.')
        for item in j.get('unknown_dispositions', []):
            if item['disposition'] == 'unresolved':
                add('unresolved_unknown', 'judgment', 'Unresolved analyzer unknown '
                    + str(item['unknown_index']) + ': ' + a['unknowns'][item['unknown_index']] + '. ' + item['reason'])
    else:
        for index, unknown in enumerate(a.get('unknowns', [])):
            add('unresolved_unknown', 'analysis', 'Analyzer unknown ' + str(index) + ': ' + unknown)
    for stage, result in [('analysis', a), ('judgment', j)]:
        for concern in result.get('unmapped_concerns', []):
            add('unmapped_concern', stage, concern['description'])
    # Stable ordering and exact deduplication make the frozen result replayable.
    unique = {json.dumps(r.model_dump(mode='json'), sort_keys=True): r for r in reasons}
    reasons = [unique[key] for key in sorted(unique)]
    assessment = ('risk_detected' if risks else 'not_assessed' if judgment['status'] != 'applied'
                  else 'uncertain' if reasons else 'clear')
    completion = ('unavailable' if analysis['status'] != 'applied' else 'partial' if reasons else 'complete')
    return SemanticSummary(completion=completion, assessment=assessment,
        requires_review=bool(risks or reasons), applicable_risks=sorted(set(risks)),
        uncertain_risks=sorted(set(uncertain)), reasons=reasons)


def selection(summary):
    summary = SemanticSummary.model_validate(summary)
    parts = []
    if summary.applicable_risks:
        parts.append('applicable risks: ' + ', '.join(r.value for r in summary.applicable_risks))
    parts.extend(reason.message for reason in summary.reasons)
    return summary.requires_review, ('; '.join(parts) + '; human review required') if parts else None


def aggregate(analysis, retrieval, judgment):
    return selection(summarize(analysis, retrieval, judgment))


def run(request, *, analysis=None, expanded=True):
    from src.db.transaction_guard import assert_no_transaction
    assert_no_transaction()
    source = request_analysis.source_text(request)
    raw = request.parameters.get('raw_prompt')
    structured_action = (request.action_class
        if not (isinstance(raw, str) and raw.strip())
        and request.action_class in {action.value for action in ActionClass} else None)
    analysis = analysis or request_analysis.analyze(source, structured_action=structured_action)
    result = dict(version=VERSION, aggregation_version=AGGREGATION_VERSION,
        source_text=source, analysis=analysis, retrieval={'status':'skipped', 'dependency':'analysis'},
        judgment={'status':'skipped', 'dependency':'retrieval'}, mode=settings.SEMANTIC_PIPELINE_MODE)
    if analysis.get('status') == 'applied':
        parsed = RequestAnalysis.model_validate(analysis['result'])
        result['handling'] = ('unsupported_action' if parsed.unsupported_actions or not parsed.actions
            else 'split_required' if len(parsed.actions) != 1 else 'action_conflict'
            if parsed.actions[0].action_class.value != request.action_class else 'single_action')
        if result['handling'] != 'single_action':
            result.update(proposed_flag=True,
                          proposed_warning='instruction/action conflict or multiple actions; split or clarify the request')
            monitoring.record('retrieval', 'skipped')
            monitoring.record('judge', 'skipped')
            return result
        with monitoring.span('retrieval') as event:
            try:
                result['retrieval'] = semantic_retrieval.retrieve(request, analysis, expanded=expanded)
            except Exception as exc:
                if not handled(exc):
                    raise
                result['retrieval'] = dict(status='unavailable', error=type(exc).__name__,
                    collection=settings.SEMANTIC_V3_COLLECTION,
                    failure=failure(exc, 'retrieval_unavailable', 'Semantic retrieval dependency or evidence validation failed.'))
            detail = result['retrieval'].get('failure') or {}
            event.update(status=result['retrieval']['status'], error_type=result['retrieval'].get('error'),
                         http_status=detail.get('http_status'), validation_errors=detail.get('validation_errors'))
        if result['retrieval'].get('candidates'):
            provider, model = request_analysis.provider_info('JUDGE')
            with monitoring.span('judge', provider=provider, model=model) as event:
                result['judgment'] = semantic_judge.judge(source, analysis, result['retrieval'])
                event.update(status=result['judgment']['status'], provider=result['judgment'].get('provider'),
                             model=result['judgment'].get('model'), error_type=result['judgment'].get('error'),
                             http_status=result['judgment'].get('http_status'),
                             validation_errors=(result['judgment'].get('failure') or {}).get('validation_errors'))
        else:
            monitoring.record('judge', 'skipped')
    else:
        monitoring.record('retrieval', 'skipped')
        monitoring.record('judge', 'skipped')
    summary = summarize(result['analysis'], result['retrieval'], result['judgment'])
    result['summary'] = summary.model_dump(mode='json')
    result['proposed_flag'], result['proposed_warning'] = selection(summary)
    return result


def validate_pipeline(pipeline):
    pair = (pipeline.get('version'), pipeline.get('aggregation_version'))
    if pair == LEGACY_PAIR:
        flag, warning = aggregate_v1(pipeline['analysis'], pipeline['retrieval'], pipeline['judgment'])
    elif pair == (VERSION, AGGREGATION_VERSION):
        PipelineEnvelope.model_validate(pipeline)
        summary = summarize(pipeline['analysis'], pipeline['retrieval'], pipeline['judgment'])
        if pipeline['analysis']['status'] == 'applied':
            parsed = RequestAnalysis.model_validate(pipeline['analysis']['result'])
            excerpts = ([a.excerpt for a in parsed.actions] + [r.excerpt for r in parsed.potential_risks]
                        + [r.excerpt for r in parsed.unmapped_concerns] + [a.excerpt for a in parsed.unsupported_actions]
                        + [claim.excerpt for claim in parsed.credential_status_claims])
            if any(not excerpt.strip() or excerpt not in pipeline['source_text'] for excerpt in excerpts):
                raise ValueError('frozen analysis contains ungrounded excerpts')
        stored = SemanticSummary.model_validate(pipeline.get('summary'))
        if summary.model_dump(mode='json') != stored.model_dump(mode='json'):
            raise ValueError('frozen semantic summary is inconsistent')
        if pipeline['judgment']['status'] == 'applied':
            semantic_judge.validate_frozen_result(pipeline['judgment'], pipeline['analysis'],
                pipeline['retrieval'], source=pipeline['source_text'])
        flag, warning = selection(summary)
    else:
        raise ValueError('unsupported frozen semantic pipeline version')
    if pipeline.get('handling') in {'split_required', 'action_conflict', 'unsupported_action'}:
        raise ValueError('invalid action handling in frozen decision')
    if (flag, warning) != (pipeline['proposed_flag'], pipeline['proposed_warning']):
        raise ValueError('frozen semantic aggregation is inconsistent')
    return flag, warning


def verify_frozen(evidence):
    """Validate the stored version, summary, and selected inputs without cloud I/O."""
    unsupported = evidence.get('unsupported_action')
    if unsupported is not None:
        from src.models.semantic import UnsupportedActionEvidence
        refusal = UnsupportedActionEvidence.model_validate(unsupported)
        semantic = evidence.get('semantic') or {}
        if (evidence.get('semantic_pipeline') is not None
                or evidence.get('semantic_flag') is not True or semantic.get('flag') is not True
                or semantic.get('source') != refusal.version or semantic.get('classification') != 'not_assessed'
                or evidence.get('semantic_warning') != refusal.reason or semantic.get('warning') != refusal.reason):
            raise ValueError('frozen unsupported-action evidence claims inconsistent semantic analysis')
    pipeline = evidence.get('semantic_pipeline')
    if pipeline is None:
        return
    flag, warning = validate_pipeline(pipeline)
    mode = pipeline.get('mode')
    if mode == 'review':
        chosen = (flag, warning)
    elif mode == 'observe':
        baseline = pipeline['baseline']
        chosen = (baseline['flag'], baseline['warning'])
    else:
        raise ValueError('unsupported frozen semantic pipeline mode')
    if chosen != (evidence.get('semantic_flag'), evidence.get('semantic_warning')):
        raise ValueError('frozen semantic decision inputs are inconsistent')
