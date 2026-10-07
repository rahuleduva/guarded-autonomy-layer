"""Advisory similarity checks with an offline pattern fallback (Phase 4)."""

from dataclasses import asdict, dataclass
from functools import lru_cache
import json
import math
import re
from typing import Any, Optional

from src.config import settings
from src.models.request import ActionRequest
from src.services import embeddings

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

    def as_evidence(self) -> dict[str, Any]:
        return asdict(self)


def _request_text(request: ActionRequest) -> str:
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
    if settings.EMBEDDING_PROVIDER == "gemini":
        return embeddings.gemini_embed(text)
    return embeddings.normalize(_embedding_model().encode(text, normalize_embeddings=True).tolist())


def _cloud_client():
    from urllib.parse import urlparse
    from qdrant_client import QdrantClient
    url = urlparse(settings.QDRANT_URL)
    if url.scheme != "https" or not (url.hostname or "").endswith(".cloud.qdrant.io"):
        raise ValueError("embedding storage requires a cloud Qdrant HTTPS endpoint")
    return QdrantClient(url=settings.QDRANT_URL, api_key=settings.QDRANT_API_KEY or None,
                        timeout=2, check_compatibility=False)


def _query_qdrant(text: str) -> tuple[float, Optional[str], Optional[str]]:
    from qdrant_client import QdrantClient, models

    vector = _vector(text)
    client = _cloud_client()
    try:
        collection = client.get_collection(settings.QDRANT_COLLECTION)
        params = collection.config.params.vectors
        if not isinstance(params, models.VectorParams) or params.distance != models.Distance.COSINE:
            raise ValueError("semantic collection requires one unnamed cosine vector")
        if params.size != len(vector):
            raise ValueError("semantic collection dimensions do not match the embedding model")
        result = client.query_points(
            collection_name=settings.QDRANT_COLLECTION,
            query=vector,
            limit=1,
            with_payload=True,
            query_filter=models.Filter(must=[models.FieldCondition(
                key="embedding_spec", match=models.MatchValue(value=embeddings.specification()))]),
        )
    finally:
        client.close()
    if not result.points:
        raise ValueError("semantic collection contains no seeded patterns")
    point = result.points[0]
    score = float(point.score)
    if (not math.isfinite(score)
            or score < -1 - _COSINE_SCORE_TOLERANCE
            or score > 1 + _COSINE_SCORE_TOLERANCE):
        raise ValueError("semantic collection returned an invalid cosine score")
    score = min(1.0, max(-1.0, score))
    payload = point.payload or {}
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        reason = "similarity to a seeded advisory pattern"
    return score, reason, str(point.id)


def assess(request: ActionRequest) -> SemanticAssessment:
    """Return a scored warning with provenance; inability to query falls back."""
    threshold = settings.SEMANTIC_THRESHOLD
    if not math.isfinite(threshold) or not 0 < threshold <= 1:
        raise ValueError("SEMANTIC_THRESHOLD must be in (0, 1]")
    text = _request_text(request)
    if settings.OFFLINE_MODE or not settings.QDRANT_URL:
        return _offline(text, threshold)

    try:
        score, reason, reference = _query_qdrant(text)
    except Exception as exc:
        # Advisory I/O must never become the authority granting/denying access.
        # Store the exception type only; network errors can contain credentials.
        return _offline(text, threshold, fallback_reason=type(exc).__name__)
    flag = score >= threshold
    return SemanticAssessment(
        flag, score, reason if flag else None,
        f"qdrant:{embeddings.specification()}", threshold, reference=reference,
    )


def evaluate(request: ActionRequest) -> tuple[bool, float, Optional[str]]:
    """Compatibility interface from C.6: flag, score, and optional warning."""
    result = assess(request)
    return result.flag, result.score, result.warning
