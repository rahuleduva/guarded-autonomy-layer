"""Bounded, aspect-aware Qdrant retrieval with per-query provenance."""
import math
from src.config import settings
from src.models.semantic import RequestAnalysis, RetrievalQuery, FAMILIES
from src.services import embeddings, monitoring
from src.services import semantic_v3_corpus as corpus_service
from src.services.request_analysis import canonical_text
from src.services.semantic_failures import failure, handled, SemanticValidationError

VERSION = 'multi-query-v2'
CROSS_OPERATION = ['review_bypass', 'secret_export', 'credential_access', 'data_exposure', 'audit_tampering']


def plan(request, analysis):
    parsed = RequestAnalysis.model_validate(analysis['result'])
    queries = [RetrievalQuery(id='canonical', text=canonical_text(request), purpose='operation',
                             action_families=[FAMILIES[request.action_class]])]
    gaps, seen = [], set()
    for risk in parsed.potential_risks:
        key = (risk.category.value, risk.query.strip().casefold())
        if key in seen:
            continue
        seen.add(key)
        if len(queries) - 1 >= settings.SEMANTIC_MAX_EXTRA_QUERIES:
            gaps.append('query_budget:' + risk.category.value)
            continue
        queries.append(RetrievalQuery(id=f'risk-{len(queries)}', text=corpus_service.text(risk.query),
                                     purpose='risk', risk_categories=[risk.category]))
    if parsed.unmapped_concerns:
        if len(queries) - 1 < settings.SEMANTIC_MAX_EXTRA_QUERIES:
            queries.append(RetrievalQuery(id='general', text=canonical_text(request), purpose='general'))
        gaps.append('unmapped_concerns')
    return queries, gaps


def retrieve(request, analysis, *, expanded=True):
    from src.db.transaction_guard import assert_no_transaction
    assert_no_transaction()
    from qdrant_client import models
    from src.services.semantic_engine import _cloud_client, _vector
    corpus = corpus_service.load()
    eligible = {c.id:c for c in corpus_service.eligible(corpus)}
    if not eligible:
        raise SemanticValidationError('v3 corpus has no eligible examples')
    queries, gaps = plan(request, analysis)
    if not expanded:
        queries, gaps = queries[:1], ['diagnostic_canonical_only']
    common = [models.FieldCondition(key=k, match=models.MatchValue(value=v)) for k,v in {
        'corpus_hash':corpus.fingerprint, 'embedding_spec':embeddings.specification(),
        'text_format':corpus_service.TEXT_FORMAT}.items()]
    common.append(models.FieldCondition(key='review_status', match=models.MatchAny(any=
        ['approved','pending_human_review'] if settings.SEMANTIC_V3_ALLOW_UNREVIEWED else ['approved'])))
    client = _cloud_client()
    candidates, query_logs = {}, []
    try:
        params = client.get_collection(settings.SEMANTIC_V3_COLLECTION).config.params.vectors
        if not isinstance(params, models.VectorParams) or params.distance != models.Distance.COSINE:
            raise SemanticValidationError('v3 collection requires unnamed cosine vectors')
        for query in queries:
            must, should = list(common), []
            if query.purpose == 'operation':
                should = [models.FieldCondition(key='action_families', match=models.MatchAny(any=query.action_families)),
                          models.FieldCondition(key='risk_aspects', match=models.MatchAny(any=CROSS_OPERATION))]
            elif query.purpose == 'risk':
                must.append(models.FieldCondition(key='risk_aspects', match=models.MatchAny(any=[r.value for r in query.risk_categories])))
            log = dict(query.model_dump(mode='json'), matches=[], searches=[],
                filters=models.Filter(must=must, should=should or None).model_dump(mode='json', exclude_none=True))
            try:
                vector = _vector(query.text)
                if params.size != len(vector):
                    raise SemanticValidationError('v3 vector dimension mismatch')
            except Exception as exc:
                if not handled(exc):
                    raise
                detail = failure(exc, 'query_unavailable', 'Query embedding failed or has incompatible dimensions.')
                log.update(status='unavailable', error=type(exc).__name__, failure=detail)
                gaps.append('query_unavailable:' + query.id)
                monitoring.record('retrieval_query', 'unavailable', error_type=type(exc).__name__,
                    http_status=detail['http_status'], validation_errors=detail['validation_errors'])
                query_logs.append(log)
                continue
            for label in ('risky', 'benign', 'uncertain'):
                filt = models.Filter(must=[*must, models.FieldCondition(key='label', match=models.MatchValue(value=label))], should=should or None)
                search = dict(label=label, filters=filt.model_dump(mode='json', exclude_none=True), matches=[])
                try:
                    result = client.query_points(collection_name=settings.SEMANTIC_V3_COLLECTION, query=vector,
                        limit=settings.SEMANTIC_CANDIDATES_PER_LABEL, with_payload=True, query_filter=filt)
                    invalid = False
                    for rank, point in enumerate(result.points, 1):
                        try:
                            payload = point.payload or {}
                            if payload.get('label') != label:
                                raise SemanticValidationError('candidate label differs from requested label')
                            ident = payload.get('example_id')
                            row = eligible.get(ident)
                            if row is None or str(point.id) != corpus_service.point_id(corpus, row):
                                raise SemanticValidationError('unknown or inconsistent v3 candidate ID')
                            expected_risks = [r.category.value for r in row.risk_aspects]
                            expected_families = sorted({a.action_family for a in row.actions})
                            if any(payload.get(k) != v for k, v in {
                                'corpus_hash':corpus.fingerprint, 'embedding_spec':embeddings.specification(),
                                'text_format':corpus_service.TEXT_FORMAT, 'instruction':row.instruction,
                                'label':row.label, 'risk_aspects':expected_risks,
                                'action_families':expected_families, 'review_status':row.review_status,
                                'findings':[r.model_dump(mode='json') for r in row.risk_aspects]}.items()):
                                raise SemanticValidationError('candidate payload differs from frozen corpus')
                            score = float(point.score)
                            if not math.isfinite(score) or not -1.000001 <= score <= 1.000001:
                                raise SemanticValidationError('invalid cosine score')
                        except (ValueError, TypeError) as exc:
                            invalid = True
                            gaps.append('evidence_invalid:' + query.id + ':' + label)
                            search.setdefault('invalid_evidence', []).append(failure(exc, 'evidence_invalid', 'A retrieved candidate failed integrity validation.'))
                            continue
                        match = dict(query_id=query.id, label=label, rank=rank, score=max(-1, min(1, score)))
                        entry = dict(example_id=ident, **match)
                        search['matches'].append(entry)
                        log['matches'].append(entry)
                        candidate = candidates.setdefault(ident, dict(example_id=ident, instruction=row.instruction,
                            label=row.label, risk_aspects=expected_risks, findings=payload['findings'],
                            review_status=row.review_status, matches=[]))
                        candidate['matches'].append(match)
                    search['status'] = 'partial' if invalid else 'applied'
                    if not result.points:
                        log.setdefault('observations', []).append('empty:' + label)
                    monitoring.record('retrieval_query', search['status'],
                        validation_errors=({'non_field_errors': [
                            {'code': item['code'], 'message': item['message']}
                            for item in search.get('invalid_evidence', [])]} if invalid else None))
                except Exception as exc:
                    if not handled(exc):
                        raise
                    detail = failure(exc, 'query_unavailable', 'Qdrant label search could not complete.')
                    search.update(status='unavailable', error=type(exc).__name__, failure=detail)
                    gaps.append('query_unavailable:' + query.id + ':' + label)
                    monitoring.record('retrieval_query', 'unavailable', error_type=type(exc).__name__,
                        http_status=detail['http_status'], validation_errors=detail['validation_errors'])
                log['searches'].append(search)
            states = [search['status'] for search in log['searches']]
            log['status'] = ('applied' if all(state == 'applied' for state in states)
                             else 'unavailable' if all(state == 'unavailable' for state in states) else 'partial')
            query_logs.append(log)
    finally:
        client.close()
    # Round-robin by query retains per-aspect coverage rather than one global top-k.
    selected=[]
    buckets=[[m['example_id'] for m in q.get('matches',[])] for q in query_logs]
    for index in range(max((len(b) for b in buckets), default=0)):
        for bucket in buckets:
            if index < len(bucket) and bucket[index] not in selected:
                selected.append(bucket[index])
    limit=settings.SEMANTIC_JUDGE_MAX_CANDIDATES
    if len(selected)>limit:
        gaps.append('candidate_budget_truncation')
    selected=selected[:limit]
    statuses = [q['status'] for q in query_logs]
    status = ('unavailable' if statuses and all(s == 'unavailable' for s in statuses)
              else 'partial' if any(s != 'applied' for s in statuses)
              else 'applied' if selected else 'no_evidence')
    return dict(status=status, version=VERSION, collection=settings.SEMANTIC_V3_COLLECTION, corpus_hash=corpus.fingerprint,
        embedding_spec=embeddings.specification(),text_format=corpus_service.TEXT_FORMAT,
        queries=query_logs,candidates=[candidates[k] for k in selected],coverage_gaps=sorted(set(gaps)),
        provisional=any(eligible[k].review_status!='approved' for k in selected))
