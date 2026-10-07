"""Configured embedding generation; no local vector storage."""
import math
from src.config import settings


def specification() -> str:
    if settings.EMBEDDING_PROVIDER == "gemini":
        return f"gemini:{settings.GEMINI_EMBEDDING_MODEL}:{settings.GEMINI_EMBEDDING_DIMENSIONS}:similarity-v1"
    return f"sentence_transformers:{settings.SENTENCE_TRANSFORMER_MODEL}:similarity-v1"


def normalize(vector) -> list[float]:
    values = [float(value) for value in vector]
    if not values or not all(math.isfinite(value) for value in values):
        raise ValueError("embedding contains invalid values")
    norm = math.sqrt(sum(value * value for value in values))
    if not norm:
        raise ValueError("embedding has zero magnitude")
    return [value / norm for value in values]


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
