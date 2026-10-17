"""Configured embedding generation; no local vector storage."""
from functools import lru_cache
import hashlib
import json
import logging
import math
from src.config import settings
from src.services import monitoring
from src.services.llm_routing import current_routing

logger = logging.getLogger(__name__)


def selected_profile():
    catalog = current_routing().embeddings
    return catalog.models[catalog.selected]


def provider_name() -> str:
    return selected_profile().provider


def specification() -> str:
    profile = selected_profile()
    if profile.provider == "gemini":
        return f"gemini:{profile.model}:{settings.GEMINI_EMBEDDING_DIMENSIONS}:similarity-v1"
    if profile.provider == "jina":
        return f"jina:{profile.model}:{settings.JINA_EMBEDDING_DIMENSIONS}:text-matching-v1"
    return f"{profile.provider}:{profile.model}:similarity-v1"


def normalize(vector) -> list[float]:
    values = [float(value) for value in vector]
    if not values or not all(math.isfinite(value) for value in values):
        raise ValueError("embedding contains invalid values")
    norm = math.sqrt(sum(value * value for value in values))
    if not norm:
        raise ValueError("embedding has zero magnitude")
    return [value / norm for value in values]


@lru_cache(maxsize=1)
def _redis_client(redis_url: str):
    import redis

    return redis.Redis.from_url(
        redis_url,
        socket_connect_timeout=1.5,
        socket_timeout=1.5,
        decode_responses=True,
    )


def query_embedding(text: str, compute) -> list[float]:
    """Return a cached query vector, computing and caching it on a miss.

    Redis is an optimization only. Connection, serialization, and cache errors
    are logged by exception type and fall through to the configured embedder.
    The cache key hashes the text so raw prompts are not stored in Redis keys.
    """
    from src.db.transaction_guard import assert_no_transaction
    assert_no_transaction()
    redis_url = settings.REDIS_URL
    key = None
    client = None
    if redis_url:
        key_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        namespace = settings.REDIS_NAMESPACE.strip().strip(":") or "guarded-autonomy-layer"
        key = f"{namespace}:query-embedding:v1:{specification()}:{key_hash}"
        try:
            client = _redis_client(redis_url)
            cached = client.get(key)
            if cached is not None:
                vector = normalize(json.loads(cached))
                expected_size = (settings.GEMINI_EMBEDDING_DIMENSIONS
                                 if provider_name() == "gemini"
                                 else settings.JINA_EMBEDDING_DIMENSIONS
                                 if provider_name() == "jina" else len(vector))
                if len(vector) == expected_size:
                    monitoring.record("embedding_cache", "hit", provider="redis")
                    return vector
                logger.warning("Redis embedding cache entry has the wrong vector size; recomputing")
                monitoring.record("embedding_cache", "degraded", provider="redis",
                                  error_type="VectorSizeMismatch")
        except Exception as exc:
            logger.warning("Redis embedding cache read skipped (%s)", type(exc).__name__)
            monitoring.record("embedding_cache", "unavailable", provider="redis",
                              error_type=type(exc).__name__)

    with monitoring.span("embedding", provider=provider_name()):
        vector = normalize(compute())
    if client is not None and key is not None:
        try:
            client.setex(key, settings.EMBEDDING_CACHE_TTL_SECONDS,
                         json.dumps(vector, separators=(",", ":")))
        except Exception as exc:
            logger.warning("Redis embedding cache write skipped (%s)", type(exc).__name__)
            monitoring.record("embedding_cache", "unavailable", provider="redis",
                              error_type=type(exc).__name__)
    return vector


def gemini_embed(text: str) -> list[float]:
    from src.db.transaction_guard import assert_no_transaction
    assert_no_transaction()
    if settings.OFFLINE_MODE or not settings.GEMINI_API_KEY:
        raise ValueError("Gemini embedding is unavailable in offline mode or without a key")
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=settings.GEMINI_API_KEY,
                          http_options=types.HttpOptions(timeout=10000))
    try:
        result = client.models.embed_content(model=selected_profile().model,
            contents=f"task: sentence similarity | query: {text}",
            config=types.EmbedContentConfig(output_dimensionality=settings.GEMINI_EMBEDDING_DIMENSIONS))
    finally:
        client.close()
    if not result.embeddings or len(result.embeddings) != 1:
        raise ValueError("embedding provider returned an unexpected result")
    if len(result.embeddings[0].values or []) != settings.GEMINI_EMBEDDING_DIMENSIONS:
        raise ValueError("embedding provider returned unexpected dimensions")
    return normalize(result.embeddings[0].values)


def jina_embed(text: str) -> list[float]:
    """Embed seeds and queries with the same Jina similarity task."""
    from src.db.transaction_guard import assert_no_transaction
    assert_no_transaction()
    import httpx

    key = settings.JINA_API_KEY
    if settings.OFFLINE_MODE or not key.strip():
        raise ValueError("Jina embedding is unavailable in offline mode or without a key")
    try:
        endpoint = httpx.URL(settings.JINA_EMBEDDING_URL)
        if endpoint.scheme != "https" or not endpoint.host or endpoint.userinfo:
            raise ValueError("invalid endpoint")
        with httpx.Client(timeout=settings.JINA_EMBEDDING_TIMEOUT_SECONDS,
                          follow_redirects=False) as client:
            response = client.post(str(endpoint),
                headers={"Authorization": "Bearer " + key},
                json={"model": selected_profile().model, "input": [text],
                      "task": "text-matching", "dimensions": settings.JINA_EMBEDDING_DIMENSIONS,
                      "embedding_type": "float", "normalized": True})
    except Exception as exc:
        raise ValueError(f"Jina embedding request failed ({type(exc).__name__})") from None
    if response.status_code != 200:
        raise ValueError(f"Jina embedding request failed (HTTP {response.status_code})")
    try:
        data = response.json()["data"]
        if len(data) != 1 or data[0]["index"] != 0:
            raise ValueError("unexpected result")
        vector = data[0]["embedding"]
        if (not isinstance(vector, list) or len(vector) != settings.JINA_EMBEDDING_DIMENSIONS
                or any(type(value) not in {int, float} for value in vector)):
            raise ValueError("unexpected vector")
        return normalize(vector)
    except Exception:
        raise ValueError("Jina embedding returned an invalid vector or unexpected dimensions") from None


def aicredits_embed(text: str) -> list[float]:
    """OpenAI-compatible embedding endpoint; model IDs come from the catalog."""
    from src.db.transaction_guard import assert_no_transaction
    assert_no_transaction()
    import httpx

    if settings.OFFLINE_MODE or not settings.AI_CREDITS_API_KEY:
        raise ValueError("AICredits embedding is unavailable in offline mode or without a key")
    endpoint = httpx.URL(settings.AI_CREDITS_API_URL.rstrip("/") + "/embeddings")
    if endpoint.scheme != "https" or not endpoint.host or endpoint.userinfo:
        raise ValueError("invalid AICredits embedding endpoint")
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        response = client.post(str(endpoint), headers={"Authorization": "Bearer " + settings.AI_CREDITS_API_KEY},
                               json={"model": selected_profile().model, "input": text})
    response.raise_for_status()
    data = response.json().get("data")
    if not isinstance(data, list) or len(data) != 1 or data[0].get("index") != 0:
        raise ValueError("AICredits embedding returned an unexpected result")
    return normalize(data[0]["embedding"])
