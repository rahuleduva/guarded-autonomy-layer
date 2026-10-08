"""Operation-filtered similarity evidence and an explicit offline pattern stub."""

from dataclasses import asdict, dataclass, field, replace
from functools import lru_cache
import json
import math
import posixpath
import re
from typing import Any, Optional

from src.config import settings
from src.models.request import ActionRequest
from src.services import embeddings, reranker
from src.services.blast_radius import estimate
from src.services.semantic_corpus import action_family, load_corpus, RISK_REASONS

EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
# Qdrant cosine scores can exceed 1 by a few ulps due to floating-point
# rounding. Accept only negligible drift, then clamp to the mathematical range.
_COSINE_SCORE_TOLERANCE = 1e-6
# Binary stub scores are intentionally separate from vector cosine scores.
_PATTERNS = (
    ("mass-deletion", r"\b(delete|remove|wipe|purge)\b.{0,80}\b(all|everything|entire|whole|recursive|recursively)\b",
     "possible mass deletion"),
    ("review-bypass", r"\b(ignore|bypass|skip|disable)\b.{0,80}\b(approval|approvals|review|policy|policies|guardrails)\b",
     "possible attempt to bypass review"),
    ("secret-export", r"\b(send|export|upload|reveal|exfiltrate)\b.{0,80}\b(secret|secrets|password|passwords|private key|api key)\b",
     "possible disclosure of credentials"),
    ("credential-target", r"(?:^|/)(?:\.env(?:\.[\w-]+)?|\.ssh/(?:id_rsa|id_ed25519)|etc/(?:passwd|shadow))(?:$|[\s\"/])",
     "possible access to credential material"),
)


@dataclass(frozen=True)
class SemanticAssessment:
    flag: bool
    score: float
    warning: Optional[str]
    source: str
    threshold: float
    reference: Optional[str] = None
    fallback_reason: Optional[str] = None
    benign_score: Optional[float] = None
    benign_reference: Optional[str] = None
    score_margin: Optional[float] = None
    required_margin: float = 0.0
    classification: str = "pattern"
    corpus_hash: Optional[str] = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    reranking: Optional[dict[str, Any]] = None

    def as_evidence(self) -> dict[str, Any]:
        return asdict(self)


def _request_text(request: ActionRequest) -> str:
    radius = estimate(request)
    scope = "recursive directory" if radius["recursive"] else (
        "wildcard selection" if radius["wildcard"] else radius["target_type"])
    parameters = {key: value for key, value in request.parameters.items()
                  if key not in {"raw_prompt", "parser_provenance"}}
    target = request.target_resource.rstrip("/")
    # Keep a sensitive parent such as .ssh, but avoid generic workspace prefixes.
    hint = posixpath.basename(target)
    if "/.ssh/" in target:
        hint = ".ssh/" + hint
    lines = [f"Operation: {request.action_class}", f"Scope: {scope}",
             f"Target: {hint}",
             "Parameters: " + json.dumps(parameters, sort_keys=True, ensure_ascii=False)]
    # The parser may omit bypass/exfiltration instructions from executable
    # arguments. Preserve that intent; never let caller-supplied context hide
    # the actual executable parameters.
    raw_prompt = request.parameters.get("raw_prompt")
    if isinstance(raw_prompt, str) and raw_prompt.strip():
        lines.append("User instruction: " + raw_prompt)
    return "\n".join(lines)


def _pattern_text(request: ActionRequest) -> str:
    # The explicit offline grammar retains its original compatibility contract.
    return "\n".join((
        request.action_class.replace("_", " "),
        request.target_resource,
        json.dumps(request.parameters, sort_keys=True, ensure_ascii=False),
    ))


def _offline(text: str, threshold: float, fallback_reason: Optional[str] = None) -> SemanticAssessment:
    for reference, pattern, warning in _PATTERNS:
        if re.search(pattern, text, re.IGNORECASE | re.DOTALL):
            return SemanticAssessment(
                True, 1.0, warning, "offline-patterns-v1", threshold,
                reference=reference, fallback_reason=fallback_reason,
            )
    return SemanticAssessment(
        False, 0.0, None, "offline-patterns-v1", threshold, fallback_reason=fallback_reason,
    )


@lru_cache(maxsize=2)
def _load_model(model_name: str):
    # Optional dependency and cached model only: never downloads in the request
    # path. Missing dependencies/model files trigger the offline fallback.
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(model_name, local_files_only=True)


def _embedding_model():
    return _load_model(settings.SENTENCE_TRANSFORMER_MODEL)


def _vector(text: str) -> list[float]:
    def compute():
        if settings.EMBEDDING_PROVIDER == "gemini":
            return embeddings.gemini_embed(text)
        if settings.EMBEDDING_PROVIDER == "jina":
            return embeddings.jina_embed(text)
        return _embedding_model().encode(text, normalize_embeddings=True).tolist()

    return embeddings.query_embedding(text, compute)


def _cloud_client():
    from urllib.parse import urlparse
    from qdrant_client import QdrantClient
    url = urlparse(settings.QDRANT_URL)
    if url.scheme != "https" or not (url.hostname or "").endswith(".cloud.qdrant.io"):
        raise ValueError("embedding storage requires a cloud Qdrant HTTPS endpoint")
    return QdrantClient(url=settings.QDRANT_URL, api_key=settings.QDRANT_API_KEY or None,
                        timeout=2, check_compatibility=False)


def _query_qdrant(request: ActionRequest) -> list[dict[str, Any]]:
    """Retrieve both labels within this operation and cross-operation risks."""
    from qdrant_client import models

    corpus = load_corpus()
    family = action_family(request.action_class)
    vector = _vector(_request_text(request))
    client = _cloud_client()
    candidates = []
    try:
        collection = client.get_collection(settings.QDRANT_COLLECTION)
        params = collection.config.params.vectors
        if not isinstance(params, models.VectorParams) or params.distance != models.Distance.COSINE:
            raise ValueError("semantic collection requires one unnamed cosine vector")
        if params.size != len(vector):
            raise ValueError("semantic collection dimensions do not match the embedding model")
        common = [
            models.FieldCondition(key="embedding_spec", match=models.MatchValue(value=embeddings.specification())),
            models.FieldCondition(key="corpus_hash", match=models.MatchValue(value=corpus.fingerprint)),
            models.FieldCondition(key="action_family", match=models.MatchAny(any=[family, "any"])),
        ]
        # Separate searches ensure that one label cannot crowd the other out
        # of a small top-k result. Both reuse the same cached query embedding.
        for label in ("risky", "benign"):
            result = client.query_points(
                collection_name=settings.QDRANT_COLLECTION, query=vector,
                limit=5, with_payload=True,
                query_filter=models.Filter(must=[*common, models.FieldCondition(
                    key="label", match=models.MatchValue(value=label))]),
            )
            if not result.points:
                raise ValueError("versioned semantic corpus is missing eligible examples")
            for point in result.points:
                payload = point.payload or {}
                if (payload.get("label") != label
                        or payload.get("action_family") not in {family, "any"}
                        or payload.get("corpus_hash") != corpus.fingerprint):
                    raise ValueError("semantic example metadata is inconsistent")
                category = payload.get("risk_category")
                if ((label == "risky" and category not in RISK_REASONS)
                        or (label == "benign" and category != "none")
                        or (category == "mass_deletion" and family != "delete")):
                    raise ValueError("semantic risk category is inconsistent")
                score = float(point.score)
                if (not math.isfinite(score) or score < -1 - _COSINE_SCORE_TOLERANCE
                        or score > 1 + _COSINE_SCORE_TOLERANCE):
                    raise ValueError("semantic collection returned an invalid cosine score")
                candidates.append({"reference": str(point.id),
                    "example_id": payload.get("example_id"), "label": label,
                    "text": payload.get("text"),
                    "action_family": payload["action_family"], "risk_category": category,
                    "score": min(1.0, max(-1.0, score))})
    finally:
        client.close()
    return sorted(candidates, key=lambda row: row["score"], reverse=True)


def classify(candidates: list[dict], threshold: float, required_margin: float) -> SemanticAssessment:
    """Similarity is advisory; an uncertain comparison requests review."""
    if not math.isfinite(threshold) or not 0 < threshold <= 1:
        raise ValueError("SEMANTIC_THRESHOLD must be in (0, 1]")
    if not math.isfinite(required_margin) or not 0 <= required_margin <= 1:
        raise ValueError("SEMANTIC_SCORE_MARGIN must be in [0, 1]")
    risky = max((row for row in candidates if row["label"] == "risky"), key=lambda row: row["score"])
    benign = max((row for row in candidates if row["label"] == "benign"), key=lambda row: row["score"])
    margin = risky["score"] - benign["score"]
    if risky["score"] < threshold:
        flag, classification, warning = False, "below_threshold", None
    elif margin > required_margin + _COSINE_SCORE_TOLERANCE:
        flag, classification = True, "risk_like"
        warning = RISK_REASONS[risky["risk_category"]]
    elif margin < -required_margin - _COSINE_SCORE_TOLERANCE:
        flag, classification, warning = False, "benign_like", None
    else:
        flag, classification = True, "ambiguous"
        warning = "similarity evidence is ambiguous; human review required"
    return SemanticAssessment(
        flag, risky["score"], warning, f"qdrant:{embeddings.specification()}", threshold,
        reference=risky["reference"], benign_score=benign["score"], benign_reference=benign["reference"],
        score_margin=margin, required_margin=required_margin, classification=classification,
        corpus_hash=load_corpus().fingerprint, candidates=candidates,
    )


def assess(request: ActionRequest) -> SemanticAssessment:
    """Return frozen similarity evidence; unavailable online evidence needs review."""
    threshold = settings.SEMANTIC_THRESHOLD
    if not math.isfinite(threshold) or not 0 < threshold <= 1:
        raise ValueError("SEMANTIC_THRESHOLD must be in (0, 1]")
    margin = settings.SEMANTIC_SCORE_MARGIN
    if not math.isfinite(margin) or not 0 <= margin <= 1:
        raise ValueError("SEMANTIC_SCORE_MARGIN must be in [0, 1]")
    if settings.OFFLINE_MODE:
        return _offline(_pattern_text(request), threshold)

    try:
        baseline = classify(_query_qdrant(request), threshold, margin)
    except Exception as exc:
        # Advisory I/O must never become the authority granting/denying access.
        # Store the exception type only; network errors can contain credentials.
        # Missing new metadata or a cloud outage must not look like a clean match.
        return SemanticAssessment(True, 0.0, "semantic advisory unavailable; human review required",
            "unavailable", threshold, fallback_reason=type(exc).__name__, classification="unavailable")
    reranking = reranker.rerank(_request_text(request), baseline.candidates)
    result = replace(baseline, reranking=reranking)
    if (reranking["status"] == "applied" and settings.JINA_RERANKER_MODE == "review"
            and reranking["preferred_label"] != "benign" and not baseline.flag):
        result = replace(result, flag=True, classification="reranker_review",
                         warning="reranker suggests risk or uncertainty; human review required")
    return result


def evaluate(request: ActionRequest) -> tuple[bool, float, Optional[str]]:
    """Compatibility interface from C.6: flag, score, and optional warning."""
    result = assess(request)
    return result.flag, result.score, result.warning
