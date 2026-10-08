"""Optional Jina reranking; failures never discard the existing assessment."""
import math

import httpx

from src.config import settings


def rerank(query: str, candidates: list[dict]) -> dict:
    evidence = {"provider": "jina", "model": settings.JINA_RERANKER_MODEL,
                "mode": settings.JINA_RERANKER_MODE, "status": "skipped"}
    if settings.OFFLINE_MODE or settings.JINA_RERANKER_MODE == "disabled":
        return {**evidence, "reason": "disabled"}
    if not settings.JINA_RERANKER_API_KEY.strip() or not settings.JINA_RERANKER_API_URL.strip():
        return {**evidence, "reason": "not_configured"}
    try:
        endpoint = httpx.URL(settings.JINA_RERANKER_API_URL)
        if endpoint.scheme != "https" or not endpoint.host or endpoint.userinfo:
            return {**evidence, "reason": "invalid_endpoint"}
        documents = [row.get("text") for row in candidates]
        if not documents or any(not isinstance(text, str) or not text.strip() for text in documents):
            return {**evidence, "reason": "documents_unavailable"}
        # No automatic retries or redirects: rate limits and bad requests
        # immediately fall back without resending credentials or prompt text.
        with httpx.Client(timeout=settings.JINA_RERANKER_TIMEOUT_SECONDS,
                          follow_redirects=False) as client:
            response = client.post(str(endpoint),
                headers={"Authorization": "Bearer " + settings.JINA_RERANKER_API_KEY},
                json={"model": settings.JINA_RERANKER_MODEL, "query": query,
                      "documents": documents, "top_n": len(documents), "return_documents": False})
        if response.status_code != 200:
            return {**evidence, "reason": f"http_{response.status_code}"}
        body = response.json()
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list) or len(results) != len(candidates):
            return {**evidence, "reason": "invalid_response"}
        seen, ranked = set(), []
        for result in results:
            if not isinstance(result, dict):
                return {**evidence, "reason": "invalid_response"}
            index, score = result.get("index"), result.get("relevance_score")
            if (type(index) is not int or index in seen or not 0 <= index < len(candidates)
                    or type(score) not in {int, float} or not math.isfinite(score) or not 0 <= score <= 1):
                return {**evidence, "reason": "invalid_response"}
            seen.add(index)
            row = candidates[index]
            ranked.append({"reference": row["reference"], "label": row["label"],
                           "risk_category": row["risk_category"], "score": float(score)})
        ranked.sort(key=lambda row: row["score"], reverse=True)
        risky = max(row["score"] for row in ranked if row["label"] == "risky")
        benign = max(row["score"] for row in ranked if row["label"] == "benign")
        preference = "ambiguous" if abs(risky - benign) <= 1e-6 else (
            "risky" if risky > benign else "benign")
        return {**evidence, "status": "applied", "ranked": ranked,
                "risky_score": risky, "benign_score": benign, "preferred_label": preference}
    except Exception as exc:
        # Never log exception messages, response bodies, URLs or credentials.
        return {**evidence, "reason": type(exc).__name__}
