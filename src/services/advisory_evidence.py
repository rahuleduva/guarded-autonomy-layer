"""Collect advisory inputs once, before deterministic policy evaluation."""

from typing import Any, Optional

from src.models.autonomy import AutonomyState
from src.models.enums import AutonomyLevel
from src.models.request import ActionRequest
from src.services import blast_radius, semantic_engine


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
    radius = blast_radius.estimate(request)
    semantic = semantic_engine.assess(request)
    return {
        "blast_radius": radius,
        "semantic_flag": semantic.flag,
        "semantic_score": semantic.score,
        "semantic_warning": semantic.warning,
        "semantic": semantic.as_evidence(),
        "environment": request.environment,
        "autonomy_snapshot": autonomy_snapshot(request, autonomy_state),
    }
