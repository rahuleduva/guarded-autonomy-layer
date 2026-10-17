"""LLM 1 extracts grounded action/risk hypotheses, never permission decisions."""
import hashlib
import json
from src.models.semantic import RequestAnalysis
from src.services.llm_routing import resolve_purpose
from src.services.llm_routing import RoutedStructuredParser, http_status
from src.services import monitoring
from src.services.semantic_failures import failure, handled, SemanticValidationError, is_validation_failure
from src.services.parser_workflow import run_parser_workflow
from src.services.parser_prompt import analysis_instructions

VERSION = 'request-analysis-v1'
SYSTEM = (
    "Return RequestAnalysis for the supplied request. Its fields are actions, "
    "unsupported_actions, intent_summary, potential_risks, unknowns, and unmapped_concerns. "
    "Put action mentions in the top-level actions list; each contains only action_class and excerpt. "
    "Do not wrap this response in an analysis object. Potential risks contain category, "
    "excerpt, and query. Unsupported actions and unmapped concerns contain description and "
    "excerpt. Do not return an executable action object or generate file contents.\n"
    + analysis_instructions()
)

# Retain the old import name without maintaining a second prompt contract.
FALLBACK_SYSTEM = SYSTEM  # Compatibility alias; every routed model uses the same contract.


def provider_info(stage):
    purpose = {'ANALYZER': 'request_analyzer', 'JUDGE': 'semantic_judge'}.get(stage)
    if purpose is None:
        raise SemanticValidationError(f'unsupported LLM stage: {stage}')
    profile = resolve_purpose(purpose)[1][0]
    return profile.provider, profile.model


def validate_result(result: RequestAnalysis, source: str, *, system=SYSTEM, schema=RequestAnalysis,
                    provider=None, model=None):
    primary_provider, primary_model = provider_info('ANALYZER')
    provider, model = provider or primary_provider, model or primary_model
    errors = {}
    collections = (
        ('actions', result.actions),
        ('potential_risks', result.potential_risks),
        ('unmapped_concerns', result.unmapped_concerns),
        ('unsupported_actions', result.unsupported_actions),
    )
    for field, rows in collections:
        for index, item in enumerate(rows):
            if not item.excerpt.strip() or item.excerpt not in source:
                errors[f'{field}.{index}.excerpt'] = [{
                    'code': 'excerpt_not_in_source' if item.excerpt.strip() else 'empty_excerpt',
                    'message': ('Copy an exact, continuous quote from the supplied instruction. '
                                'Do not paraphrase or add words.')
                }]
    if errors:
        raise SemanticValidationError(
            'analysis contains ungrounded excerpts',
            failure_category='grounding_invalid', validation_errors=errors)
    return dict(version=VERSION, provider=provider, model=model,
                prompt_hash=hashlib.sha256(system.encode()).hexdigest(),
                schema_hash=hashlib.sha256(json.dumps(schema.model_json_schema(),sort_keys=True).encode()).hexdigest(),
                source_hash=hashlib.sha256(source.encode()).hexdigest(),
                status='applied', result=result.model_dump(mode='json'))


def analyze(source: str):
    from src.db.transaction_guard import assert_no_transaction
    assert_no_transaction()
    provider, model = provider_info('ANALYZER')
    system = SYSTEM
    parser = RoutedStructuredParser('request_analyzer', RequestAnalysis)
    first_profile = parser.profiles[0]
    attempt_history = []

    def parse_and_validate(value):
        result = RequestAnalysis.model_validate(value)
        evidence = validate_result(
            result, source, system=SYSTEM, schema=RequestAnalysis,
            provider=parser.last_profile.provider, model=parser.last_profile.model)
        return result, evidence

    def on_attempt(attempt, status, exc, duration_ms, validation_errors, trace):
        profile = parser.last_profile
        failure_category = trace.get('failure_category')
        attempt_history.append({
            'attempt': attempt, 'status': status, 'profile': profile,
            'error': exc, 'failure_category': failure_category,
            'http_status': http_status(exc) if exc else None,
        })
        if status == 'applied':
            stage = 'analyzer'
        elif trace.get('error_origin') == 'internal':
            stage = 'analyzer_internal_failure'
        elif trace.get('error_origin') == 'provider':
            stage = 'analyzer_primary' if profile.name == first_profile.name else 'analyzer_fallback'
        else:
            stage = 'analyzer_correction'
        monitoring.record_attempt(stage, status, duration_ms=duration_ms,
                          provider=profile.provider, model=profile.model,
                          error_type=type(exc).__name__ if exc else None,
                          http_status=http_status(exc) if exc else None,
                          validation_errors=validation_errors, parser_trace=trace)

    def fallback_from():
        if parser.last_profile.name == first_profile.name:
            return None
        failed = next((item for item in attempt_history
                       if item['status'] != 'applied' and item['profile'].name == first_profile.name), None)
        failed = failed or next((item for item in attempt_history if item['status'] != 'applied'), None)
        if failed is None:
            return None
        return {'provider': failed['profile'].provider, 'model': failed['profile'].model,
                'http_status': failed['http_status']}

    with monitoring.span('analyzer', provider=provider, model=model) as event:
        try:
            if not source.strip() or len(source) > 32768:
                raise SemanticValidationError('analysis input must contain 1-32768 characters',
                                               failure_category='application_error')
            workflow = run_parser_workflow(
                parser=parser,
                messages=[{'role': 'system', 'content': SYSTEM},
                          {'role': 'user', 'content': source}],
                parse_value=parse_and_validate,
                validate_action=lambda _result: None,
                classify_validation_error=lambda exc: 'retry' if is_validation_failure(exc) else 'internal',
                on_attempt=on_attempt,
                max_output_attempts=parser.purpose.max_output_attempts,
                max_provider_retries_per_attempt=parser.purpose.max_provider_retries_per_attempt,
                stage_timeout_seconds=parser.purpose.stage_timeout_seconds,
                retry_provider_error=parser.can_retry,
            )
            evidence = workflow.analysis
            provider, model = parser.last_profile.provider, parser.last_profile.model
            evidence['attempts'] = workflow.attempts
            evidence['output_attempts'] = workflow.output_attempts
            evidence['provider_calls'] = workflow.attempts
            fallback = fallback_from()
            if fallback:
                evidence['fallback_from'] = fallback
            event.update(provider=provider, model=model)
            return evidence
        except Exception as exc:
            if not handled(exc):
                raise
            invalid = is_validation_failure(exc)
            detail = failure(exc, 'analysis_invalid' if invalid else 'analysis_unavailable',
                             'Request analysis failed schema or grounding validation.' if invalid
                             else 'Request analysis provider could not complete the request.')
            provider, model = parser.last_profile.provider, parser.last_profile.model
            fallback = fallback_from()
            event.update(status='unavailable', provider=provider, model=model,
                         error_type=type(exc).__name__,
                         http_status=http_status(exc), validation_errors=detail['validation_errors'])
            result = dict(version=VERSION, provider=provider, model=model,
                        prompt_hash=hashlib.sha256(system.encode()).hexdigest(),
                        schema_hash=hashlib.sha256(json.dumps(RequestAnalysis.model_json_schema(),sort_keys=True).encode()).hexdigest(),
                        source_hash=hashlib.sha256(source.encode()).hexdigest(),
                        status='unavailable', error=type(exc).__name__,
                        http_status=http_status(exc), attempts=parser.calls, provider_calls=parser.calls,
                        output_attempts=getattr(exc, 'parser_output_attempts', 0),
                        fallback_from=fallback, failure=detail)
            return result


def source_text(request):
    # Values are data, even when they contain fake role markers or instructions.
    params = {k: v for k, v in request.parameters.items() if k not in {'parser_provenance', 'raw_prompt'}}
    authoritative = json.dumps({'action_class':request.action_class, 'target_resource':request.target_resource,
                      'parameters':params, 'environment':request.environment,
                      },
                     ensure_ascii=False, sort_keys=True, allow_nan=False)
    raw = request.parameters.get('raw_prompt')
    return ('Original instruction:\n' + raw + '\n' if isinstance(raw,str) else '') + 'Authoritative action JSON:\n' + authoritative


def canonical_text(request):
    return 'Instruction: ' + source_text(request)
