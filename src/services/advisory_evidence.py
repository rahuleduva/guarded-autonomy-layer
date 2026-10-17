"""Collect advisory inputs once, before deterministic policy evaluation."""

from typing import Any, Optional

from src.models.autonomy import AutonomyState
from src.config import settings
from src.models.enums import AutonomyLevel
from src.models.request import ActionRequest
from src.services import blast_radius, monitoring, semantic_engine


def autonomy_snapshot(
    request: ActionRequest,
    autonomy_state: Optional[AutonomyState] = None,
) -> dict[str, Any]:
    """Serialize the exact autonomy input using the evaluator's safe default."""
    try:
        mode = AutonomyLevel(autonomy_state.mode) if autonomy_state else AutonomyLevel.SHADOW
    except ValueError:
        mode = AutonomyLevel.SHADOW
    return {
        "agent_id": request.agent_id,
        "action_class": request.action_class,
        "mode": mode.value,
        "streak": autonomy_state.streak if autonomy_state else 0,
    }


def build_evidence(
    request: ActionRequest,
    autonomy_state: Optional[AutonomyState] = None,
) -> dict[str, Any]:
    """Freeze the metrics, warning, environment, and autonomy used by a decision."""
    # The keyless baseline is pure local calculation and performs no slow I/O.
    if not settings.OFFLINE_MODE or settings.SEMANTIC_PIPELINE_MODE != 'baseline':
        from src.db.transaction_guard import assert_no_transaction
        assert_no_transaction()
    with monitoring.span("blast_radius"):
        radius = blast_radius.estimate(request)
    with monitoring.span("semantic_baseline") as event:
        semantic = semantic_engine.assess(request)
        if semantic.classification == "unavailable":
            event.update(status="unavailable", error_type=semantic.fallback_reason)
    if semantic.reranking is not None:
        rerank = semantic.reranking
        monitoring.record("reranker", rerank.get("status", "unavailable"),
                          provider=rerank.get("provider"), model=rerank.get("model"),
                          error_type=rerank.get("reason") if rerank.get("status") == "unavailable" else None)
    evidence = {
        "blast_radius": radius,
        "semantic_flag": semantic.flag,
        "semantic_score": semantic.score,
        "semantic_warning": semantic.warning,
        "semantic": semantic.as_evidence(),
        "environment": request.environment,
        "autonomy_snapshot": autonomy_snapshot(request, autonomy_state),
    }

    if settings.SEMANTIC_PIPELINE_MODE != "baseline":
        from src.services.semantic_pipeline import run
        pipeline = run(request, analysis=request._semantic_analysis)
        pipeline["baseline"] = semantic.as_evidence()
        if pipeline.get("handling") in {"split_required", "action_conflict", "unsupported_action"}:
            monitoring.record("semantic_handling", "rejected",
                              error_type=pipeline["handling"])
            raise ValueError(pipeline["proposed_warning"])
        from src.services.semantic_pipeline import validate_pipeline
        validate_pipeline(pipeline)
        evidence["semantic_pipeline"] = pipeline
        if settings.SEMANTIC_PIPELINE_MODE == "review":
            evidence["semantic_flag"] = pipeline["proposed_flag"]
            evidence["semantic_warning"] = pipeline["proposed_warning"]
    return evidence
