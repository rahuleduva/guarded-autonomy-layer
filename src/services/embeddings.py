"""Configured embedding generation; no local vector storage."""
from functools import lru_cache
import hashlib
import json
import logging
import math
from src.config import settings

logger = logging.getLogger(__name__)


def specification() -> str:
    if settings.EMBEDDING_PROVIDER == "gemini":
        return f"gemini:{settings.GEMINI_EMBEDDING_MODEL}:{settings.GEMINI_EMBEDDING_DIMENSIONS}:similarity-v1"
    if settings.EMBEDDING_PROVIDER == "jina":
        return f"jina:{settings.JINA_EMBEDDING_MODEL}:{settings.JINA_EMBEDDING_DIMENSIONS}:text-matching-v1"
    return f"sentence_transformers:{settings.SENTENCE_TRANSFORMER_MODEL}:similarity-v1"


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
                                 if settings.EMBEDDING_PROVIDER == "gemini"
                                 else settings.JINA_EMBEDDING_DIMENSIONS
                                 if settings.EMBEDDING_PROVIDER == "jina" else len(vector))
                if len(vector) == expected_size:
                    return vector
                logger.warning("Redis embedding cache entry has the wrong vector size; recomputing")
        except Exception as exc:
            logger.warning("Redis embedding cache read skipped (%s)", type(exc).__name__)

    vector = normalize(compute())
    if client is not None and key is not None:
        try:
            client.setex(key, settings.EMBEDDING_CACHE_TTL_SECONDS,
                         json.dumps(vector, separators=(",", ":")))
        except Exception as exc:
            logger.warning("Redis embedding cache write skipped (%s)", type(exc).__name__)
    return vector


def gemini_embed(text: str) -> list[float]:
    if settings.OFFLINE_MODE or not settings.GEMINI_API_KEY:
        raise ValueError("Gemini embedding is unavailable in offline mode or without a key")
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=settings.GEMINI_API_KEY,
                          http_options=types.HttpOptions(timeout=10000))
    try:
        result = client.models.embed_content(model=settings.GEMINI_EMBEDDING_MODEL,
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
                json={"model": settings.JINA_EMBEDDING_MODEL, "input": [text],
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
