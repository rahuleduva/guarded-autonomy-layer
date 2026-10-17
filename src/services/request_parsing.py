"""Shared validated parser adapter used by both API and worker processes."""
from src.services import llm_parser, monitoring


def parse_for_evaluation(request):
    from src.services.llm_routing import resolve_purpose
    profile = resolve_purpose('request_parser')[1][0]
    with monitoring.span('parser', provider=profile.provider, model=profile.model) as event:
        try:
            parsed = llm_parser.parse(request)
        except (llm_parser.UnverifiedActionError, llm_parser.OnlineActionParseError) as exc:
            analysis = getattr(exc, 'analysis', None)
            if analysis is not None:
                monitoring.record('analyzer', analysis.get('status', 'unavailable'),
                                  provider=analysis.get('provider'), model=analysis.get('model'),
                                  error_type=analysis.get('error'), http_status=analysis.get('http_status'))
            raise
        analysis = parsed._semantic_analysis
        provenance = parsed.parameters.get('parser_provenance') or {}
        actual = provenance.get('source')
        if isinstance(actual, str) and ':' in actual:
            event.update(provider=actual.split(':', 1)[0], model=actual.split(':', 1)[1])
    if analysis is not None:
        monitoring.record('analyzer', analysis.get('status', 'unavailable'),
                          provider=analysis.get('provider'), model=analysis.get('model'),
                          error_type=analysis.get('error'), http_status=analysis.get('http_status'))
    return parsed
