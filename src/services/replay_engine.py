"""Read-only replay from immutable request, policy, and advisory/autonomy inputs."""
from sqlmodel import Session, select
from src.models.autonomy import AutonomyState
from src.models.decision import DecisionRecord
from src.models.policy import Policy
from src.models.request import ActionRequest, ReplayResult
from src.services.policy_engine import evaluate
from src.services.policy_loader import canonical_policy_hash


def replay(session: Session, decision_id: str) -> ReplayResult:
    with session.no_autoflush:
        record = session.get(DecisionRecord, decision_id)
        if record is None:
            raise ValueError("decision not found")
        result = dict(decision_id=decision_id, original_outcome=record.outcome,
                      replayed_outcome=record.outcome, policy_hash=record.policy_hash,
                      is_consistent=False)
        try:
            policy = session.exec(select(Policy).where(Policy.policy_hash == record.policy_hash)).one_or_none()
            if policy is None or canonical_policy_hash(policy.rules_json) != record.policy_hash:
                raise ValueError("stored policy is missing or its content hash does not match")
            request = ActionRequest.model_validate(record.request_json)
            snapshot = record.evidence_json.get("autonomy_snapshot")
            if not snapshot or record.evidence_json.get("environment") != request.environment:
                raise ValueError("frozen autonomy or environment evidence is missing or inconsistent")
            state = AutonomyState(**snapshot)
            replayed = evaluate(request, policy, record.evidence_json, state)
            result["replayed_outcome"] = replayed.outcome
            matches = (replayed.outcome.value == record.outcome and replayed.risk_level.value == record.risk_level
                       and replayed.autonomy_level.value == record.autonomy_level and replayed.reason == record.reason
                       and replayed.blast_radius == record.evidence_json.get("blast_radius", {}))
            result.update(is_consistent=matches, discrepancy_reason=None if matches else "outcome, risk, autonomy, reason, or blast evidence differs")
        except (ValueError, TypeError, KeyError) as exc:
            result["discrepancy_reason"] = str(exc)
        return ReplayResult(**result)
