"""Token-consuming execution against database-backed synthetic resources."""
import copy
import hashlib
from uuid import uuid4
from sqlmodel import Session, select
from src.models.decision import DecisionRecord
from src.models.enums import DecisionOutcome, DecisionStage
from src.services.decision_ledger import validate_policy_record
from src.models.policy import Policy
from src.models.request import ActionRequest, ExecutionResult, RollbackResult
from src.models.runtime import ExecutionRecord, SimulatedResource
from src.models.token import ConsumedToken
from src.services import token_issuer
from src.services.path_guard import is_within_sandbox, to_absolute
from src.services.policy_loader import canonical_policy_hash


def _snapshot(session: Session, keys: list[str]) -> dict:
    rows = session.exec(select(SimulatedResource).where(
        SimulatedResource.resource.in_(keys)).with_for_update()).all()
    found = {row.resource: copy.deepcopy(row.value_json) for row in rows}
    return {key: found.get(key) for key in keys}


def _plan(session: Session, decision: DecisionRecord, request: ActionRequest, root: str):
    target = to_absolute(root, request.target_resource)
    if target is None or not is_within_sandbox(target, root):
        raise ValueError("target is outside the sandbox")
    compensation = decision.evidence_json.get("compensation")
    if compensation:
        original = session.exec(select(ExecutionRecord).where(
            ExecutionRecord.execution_id == compensation["execution_id"]).with_for_update()).one_or_none()
        if original is None or original.status != "EXECUTED":
            raise ValueError("execution cannot be compensated again")
        if target not in original.after_state:
            raise ValueError("compensation target is not bound to the original execution")
        keys = list(original.after_state)
        if any(not is_within_sandbox(key, root) for key in keys):
            raise ValueError("compensation scope is outside the sandbox")
        before = _snapshot(session, keys)
        if before != original.after_state:
            raise ValueError("resources changed since execution; compensation would overwrite newer work")
        return before, copy.deepcopy(original.before_state), original

    keys = [target]
    destination = None
    if request.action_class == "move_file":
        raw = request.parameters.get("destination")
        if not isinstance(raw, str):
            raise ValueError("move requires a destination")
        destination = to_absolute(root, raw)
        if not destination or not is_within_sandbox(destination, root) or destination == target:
            raise ValueError("invalid move destination")
        keys.append(destination)
    if request.action_class == "delete_directory":
        all_rows = session.exec(select(SimulatedResource).with_for_update()).all()
        children = [row.resource for row in all_rows if row.resource.startswith(target.rstrip("/") + "/")]
        if children and request.parameters.get("recursive") is not True:
            raise ValueError("directory is not empty")
        keys.extend(children)
    before = _snapshot(session, keys)
    after = copy.deepcopy(before)
    current = before[target]
    action = request.action_class
    if action in {"create_file", "create_directory"}:
        if current is not None:
            raise ValueError("resource already exists")
        after[target] = {"kind": "directory"} if action == "create_directory" else {"kind": "file", "content": request.parameters.get("content", "")}
    elif action in {"read_file", "update_file", "delete_file", "move_file"}:
        if current is None or current.get("kind") != "file":
            raise ValueError("file does not exist")
        if action == "update_file":
            after[target] = {"kind": "file", "content": request.parameters.get("content", "")}
        elif action == "delete_file":
            after[target] = None
        elif action == "move_file":
            if before[destination] is not None:
                raise ValueError("destination already exists")
            after[destination], after[target] = current, None
    elif action == "delete_directory":
        if current is None or current.get("kind") != "directory":
            raise ValueError("directory does not exist")
        after = {key: None for key in keys}
    else:
        raise ValueError("unsupported simulated action")
    return before, after, None


def execute(session: Session, decision_id: str, token: str) -> ExecutionResult:
    def rejected():
        return ExecutionResult(execution_id=str(uuid4()), decision_id=decision_id,
                               status="REJECTED", before_state={}, after_state={})
    claims = token_issuer.verify(token)
    decision = session.exec(select(DecisionRecord).where(
        DecisionRecord.decision_id == decision_id).with_for_update()).one_or_none()
    if (claims is None or decision is None or decision.decision_stage != DecisionStage.POLICY.value
            or decision.outcome != DecisionOutcome.ALLOWED.value):
        return rejected()
    try:
        validate_policy_record(decision)
    except ValueError:
        return rejected()
    if hashlib.sha256(token.encode()).hexdigest() != decision.capability_token_hash:
        return rejected()
    request = ActionRequest.model_validate(decision.request_json)
    expected = dict(decision_id=decision_id, policy_hash=decision.policy_hash, agent_id=request.agent_id,
                    actor_role=request.actor_role, action_class=request.action_class,
                    target_resource=request.target_resource)
    if any(claims.get(key) != value for key, value in expected.items()):
        return rejected()
    if session.get(ConsumedToken, claims["jti"]) is not None:
        return rejected()
    policy = session.exec(select(Policy).where(Policy.policy_hash == decision.policy_hash)).one_or_none()
    if policy is None or canonical_policy_hash(policy.rules_json) != decision.policy_hash:
        return rejected()
    try:
        before, after, compensation = _plan(session, decision, request, policy.rules_json["sandbox_root"])
    except (ValueError, KeyError):
        return rejected()
    session.add(ConsumedToken(nonce=claims["jti"], decision_id=decision_id))
    session.flush()  # atomic nonce insert precedes every simulated mutation
    for key, value in after.items():
        row = session.get(SimulatedResource, key)
        if value is None:
            if row is not None:
                session.delete(row)
        elif row is None:
            session.add(SimulatedResource(resource=key, value_json=value))
        else:
            row.value_json = value
            session.add(row)
    execution = ExecutionRecord(decision_id=decision_id, before_state=before, after_state=after)
    session.add(execution)
    if compensation is not None:
        compensation.status = "COMPENSATED"
        compensation.compensation_decision_id = decision_id
        session.add(compensation)
    session.flush()
    return ExecutionResult(execution_id=execution.execution_id, decision_id=decision_id,
                           status="EXECUTED", before_state=before, after_state=after)


def rollback_context(session: Session, execution_id: str) -> dict | None:
    """Snapshot rollback inputs in a short transaction, without cloud work."""
    original = session.exec(select(ExecutionRecord).where(
        ExecutionRecord.execution_id == execution_id).with_for_update()).one_or_none()
    if original is None or original.status != "EXECUTED":
        return None
    decision = session.get(DecisionRecord, original.decision_id)
    if decision is None:
        return None
    validate_policy_record(decision)
    if _snapshot(session, list(original.after_state)) != original.after_state:
        return None
    request = ActionRequest.model_validate(decision.request_json)
    compensation_request = ActionRequest(agent_id=request.agent_id, actor_role=request.actor_role,
        action_class="update_file", target_resource=request.target_resource,
        environment=request.environment, parameters={"compensation_for": execution_id})
    return dict(request=compensation_request.model_dump(mode='json'),
                original_decision_id=original.decision_id,
                after_state=copy.deepcopy(original.after_state),
                before_state=copy.deepcopy(original.before_state))


def finalize_rollback(session: Session, execution_id: str, context: dict, evidence: dict):
    """Recheck the snapshot, authorize, and mutate simulated resources atomically."""
    from src.services.orchestrator import evaluate_action
    original = session.exec(select(ExecutionRecord).where(
        ExecutionRecord.execution_id == execution_id).with_for_update()).one_or_none()
    if (original is None or original.status != 'EXECUTED'
            or original.decision_id != context['original_decision_id']
            or original.before_state != context['before_state']
            or original.after_state != context['after_state']
            or _snapshot(session, list(original.after_state)) != context['after_state']):
        return RollbackResult(execution_id=execution_id, status='FAILED', restored_state={}), None
    authorized = evaluate_action(session, ActionRequest.model_validate(context['request']),
        compensation={'execution_id': execution_id}, evidence=evidence)
    if authorized.decision != DecisionOutcome.ALLOWED:
        held = authorized.decision in {DecisionOutcome.ESCALATED, DecisionOutcome.PENDING_APPROVAL}
        return RollbackResult(
            execution_id=execution_id,
            status='PENDING_APPROVAL' if held else 'FAILED',
            compensation_decision_id=authorized.decision_id if held else None,
            restored_state={},
        ), (authorized.decision_id if held else None)
    result = execute(session, authorized.decision_id, authorized.capability_token)
    return RollbackResult(execution_id=execution_id,
        status='COMPENSATED' if result.status == 'EXECUTED' else 'FAILED',
        restored_state=result.after_state if result.status == 'EXECUTED' else {}), None


def rollback(session: Session, execution_id: str, *, context=None, evidence=None) -> RollbackResult:
    """Database-only compatibility entry point; staged callers supply frozen evidence."""
    if context is None or evidence is None:
        from src.config import settings
        if not settings.OFFLINE_MODE or settings.SEMANTIC_PIPELINE_MODE != 'baseline':
            raise ValueError('rollback requires staged context and evidence; use workflow_flows.rollback')
        # Compatibility for the pure keyless baseline: no provider/cache/vector
        # calls occur here. Online rollback always uses durable staged work.
        from src.services.advisory_evidence import build_evidence
        context = rollback_context(session, execution_id)
        if context is None:
            return RollbackResult(execution_id=execution_id, status='FAILED', restored_state={})
        evidence = build_evidence(ActionRequest.model_validate(context['request']))
    return finalize_rollback(session, execution_id, context, evidence)[0]
