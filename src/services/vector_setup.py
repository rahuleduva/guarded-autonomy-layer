"""Explicit cloud seeding and measured calibration; never run at app startup."""
import json
import math
import time
from uuid import NAMESPACE_URL, uuid5
from src.config import PROJECT_ROOT, settings
from src.services import embeddings, semantic_engine
from src.services.semantic_corpus import example_request, load_corpus

def point_id(example_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, embeddings.specification() + load_corpus().fingerprint + example_id))


def seed() -> dict:
    from qdrant_client import models
    if settings.OFFLINE_MODE:
        raise ValueError("cloud seeding requires OFFLINE_MODE=false")
    corpus = load_corpus()
    vectors = []
    for example in corpus.examples:
        vectors.append(semantic_engine._vector(semantic_engine._request_text(example_request(example.request))))
        time.sleep(0.25)
    dimension = len(vectors[0])
    if any(len(vector) != dimension for vector in vectors):
        raise ValueError("embedding dimensions changed during seeding")
    client = semantic_engine._cloud_client()
    try:
        if not client.collection_exists(settings.QDRANT_COLLECTION):
            client.create_collection(settings.QDRANT_COLLECTION,
                vectors_config=models.VectorParams(size=dimension, distance=models.Distance.COSINE))
        params = client.get_collection(settings.QDRANT_COLLECTION).config.params.vectors
        if not isinstance(params, models.VectorParams) or params.size != dimension or params.distance != models.Distance.COSINE:
            raise ValueError("collection must use the configured dimensions and cosine distance; use another collection name")
        for key in ("embedding_spec", "corpus_hash", "action_family", "label"):
            client.create_payload_index(settings.QDRANT_COLLECTION, key,
                field_schema=models.PayloadSchemaType.KEYWORD, wait=True)
        points = [models.PointStruct(id=point_id(example.id), vector=vector, payload={
            "text": semantic_engine._request_text(example_request(example.request)),
            "example_id": example.id, "action_family": example.action_family,
            "label": example.label, "risk_category": example.risk_category,
            "corpus_version": corpus.version, "corpus_hash": corpus.fingerprint,
            "embedding_spec": embeddings.specification(), "reviewed": corpus.reviewed,
            "origin": corpus.origin,
        }) for example, vector in zip(corpus.examples, vectors)]
        client.upsert(settings.QDRANT_COLLECTION, points=points, wait=True)
        stored = client.retrieve(settings.QDRANT_COLLECTION,
            ids=[point.id for point in points], with_payload=True, with_vectors=False)
        if (len(stored) != len(points)
                or any((point.payload or {}).get("corpus_hash") != corpus.fingerprint for point in stored)):
            raise ValueError("cloud seed verification failed")
    finally:
        client.close()
    return {"collection": settings.QDRANT_COLLECTION, "embedding_spec": embeddings.specification(),
        "corpus_version": corpus.version, "corpus_hash": corpus.fingerprint,
        "dimensions": dimension, "seeded_patterns": len(points), "reviewed": corpus.reviewed}


def _metrics(rows: list[dict], threshold: float, margin: float) -> dict:
    counts = {"true_positive": 0, "true_negative": 0, "false_positive": 0, "false_negative": 0}
    for row in rows:
        flag = semantic_engine.classify(row["candidates"], threshold, margin).flag
        key = ("true_" if flag == row["expected_flag"] else "false_") + ("positive" if flag else "negative")
        counts[key] += 1
    counts["errors"] = counts["false_positive"] + counts["false_negative"]
    return counts


def calibrate() -> dict:
    if settings.OFFLINE_MODE:
        raise ValueError("vector calibration requires OFFLINE_MODE=false and seeded cloud patterns")
    cases = json.loads((PROJECT_ROOT / "examples/semantic_action_validation_cases.json").read_text())
    measured = []
    if {case["split"] for case in cases} != {"calibration", "holdout"}:
        raise ValueError("calibration requires separate calibration and holdout cases")
    for case in cases:
        request = example_request(case["request"])
        matches = semantic_engine._query_qdrant(request)
        assessment = semantic_engine.classify(matches, settings.SEMANTIC_THRESHOLD, settings.SEMANTIC_SCORE_MARGIN)
        measured.append({**case, "input_text": semantic_engine._request_text(request),
                         **assessment.as_evidence()})
        time.sleep(0.25)
    training = [row for row in measured if row["split"] == "calibration"]
    holdout = [row for row in measured if row["split"] == "holdout"]
    scores = sorted(set(row["score"] for row in training))
    cutoffs = {settings.SEMANTIC_THRESHOLD, 1.0}
    cutoffs.update(score for score in scores if score > 0)
    cutoffs.update(math.nextafter(score, 1.0) for score in scores if 0 < score < 1)
    cutoffs.update((a + b) / 2 for a, b in zip(scores, scores[1:]) if a + b > 0)
    margin = settings.SEMANTIC_SCORE_MARGIN
    suggested = min(cutoffs, key=lambda cutoff: (
        _metrics(training, cutoff, margin)["false_negative"],
        _metrics(training, cutoff, margin)["false_positive"],
        abs(cutoff - settings.SEMANTIC_THRESHOLD),
    ))
    for row in measured:
        result = semantic_engine.classify(row["candidates"], suggested, margin)
        row["suggested_flag"] = result.flag
        row["suggested_classification"] = result.classification
    return {"embedding_spec": embeddings.specification(), "corpus_hash": load_corpus().fingerprint,
        "current_threshold": settings.SEMANTIC_THRESHOLD, "required_margin": margin,
        "suggested_threshold": suggested, "applied": False,
        "calibration": {"cases": len(training), "current": _metrics(training, settings.SEMANTIC_THRESHOLD, margin),
                        "suggested": _metrics(training, suggested, margin)},
        "holdout": {"cases": len(holdout), "current": _metrics(holdout, settings.SEMANTIC_THRESHOLD, margin),
                    "suggested": _metrics(holdout, suggested, margin)},
        "cases": measured,
        "limitation": "Small synthetic corpus and evaluation set; domain review and representative traffic are still required. Margin is fixed, not calibrated."}
