"""Pure, deterministic policy evaluation (Phase 2, step 3).

``evaluate`` is the single decision point. It interprets the policy artifact and
nothing else: no code constants carry risk levels, permissions, TTLs, deny
patterns, or environment rules. Change any of those in the artifact and the
behaviour changes with it, because ``policy_hash`` changes with it (C.4).

It is **pure**: no database session, no clock read, no network, no randomness.
The policy row is read for ``rules_json`` and ``policy_hash`` only. Given the
same request, the same policy, the same frozen evidence, and the same autonomy
state, it returns an identical result every time -- which is exactly the Phase 2
DoD and the precondition for replay (A2).

Layers, in fixed precedence order. Denials are resolved most-fundamental first
so the recorded ``reason`` is reproducible, not just the outcome:

1. **Structure** -- a key the engine needs is absent or empty. The loader
   rejects such artifacts (step 2); reaching this branch means the engine was
   handed something it cannot interpret, so it denies rather than guesses.
2. **Role** -- ``actor_role`` absent from ``role_permissions``, or the
   ``action_class`` not granted to that role.
3. **Scope** -- the target resolves outside the policy's ``sandbox_root``.
   Pure algebra via :mod:`src.services.path_guard`; regex never owns this
   boundary (C.8).
4. **Content** -- any ``regex_deny_rules`` pattern matches. Deny-only, so
   "deny before allow, most-specific first" reduces to: any match denies, and
   artifact order decides which reason is recorded.
5. **Environment** -- a destructive action outside its allowed environments
   escalates for human review (B7).
6. **Token viability** -- a ``ttl_map`` value of ``null`` means no token is
   issuable, so the action escalates. It is never minted as a born-expired
   token (C.4).
7. **Advisory evidence** -- a frozen semantic warning or broad blast radius
   escalates for review; neither grants permission nor changes the risk map.
8. **Autonomy** -- *inside* this function, not applied downstream. What the
   autonomy state does with an otherwise-permitted action: ``SHADOW`` logs
   without executing, ``ASSISTED`` holds for sign-off, ``LIVE`` allows.

Three deliberate boundaries:

- **Autonomy never grants what policy forbids.** Steps 1-7 run first and
  return their outcome unchanged. The autonomy gate only subdivides an action
  the policy already permits, which is what keeps ``PENDING_APPROVAL``
  (autonomy-held) distinct from ``ESCALATED`` (policy-forced) -- the clean
  separation B8 asks for.
- **Evidence is echoed, never recomputed.** ``blast_radius`` and ``evidence``
  are copied verbatim out of the frozen evidence bundle. Recomputing them here
  would drift on replay (A.2); this phase does not own those metrics (Phase 4).
- **An unreadable artifact denies; it never partially applies.** A deny rule
  that will not compile, a permission list that is not a list, a missing
  required key -- each is a denial, not a skipped check. The alternative is
  silently narrowing the policy's intent, and a deny rule that quietly stops
  firing is an allow.

The engine decides the TTL and never mints a token (C.6): ``ttl_seconds`` is
populated only for an ``ALLOWED`` result, so no downstream caller can issue a
capability token for a denied, escalated, or held decision.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional

from src.models.autonomy import AutonomyState
from src.models.policy import Policy
from src.models.enums import AutonomyLevel, DecisionOutcome, RiskLevel
from src.models.request import ActionRequest, EvaluationResult
from src.services.path_guard import is_within_sandbox, to_absolute
from src.services.action_binding import fingerprint

DENY_RULE_FIELDS = ("pattern", "reason")

_MAX_PARAMETER_DEPTH = 12


class PolicyInterpretationError(ValueError):
    """The artifact declares something the engine cannot faithfully interpret.

    Raised instead of guessing. The outer handler turns it into a denial, so a
    rule the engine cannot compile or read denies the action rather than being
    skipped -- an unreadable deny rule must never become an allow.
    """


def _deny(
    request: ActionRequest,
    policy: Policy,
    evidence: Dict[str, Any],
    reason: str,
    risk_level: RiskLevel = RiskLevel.CRITICAL,
    autonomy_level: AutonomyLevel = AutonomyLevel.SHADOW,
) -> EvaluationResult:
    """Build a ``DENIED`` result. ``ttl_seconds`` stays ``None`` -- never minted."""
    return _result(
        request,
        policy,
        evidence,
        DecisionOutcome.DENIED,
        risk_level,
        reason,
        None,
        autonomy_level,
    )


def _result(
    request: ActionRequest,
    policy: Policy,
    evidence: Dict[str, Any],
    outcome: DecisionOutcome,
    risk_level: RiskLevel,
    reason: str,
    ttl_seconds: Optional[int],
    autonomy_level: AutonomyLevel,
) -> EvaluationResult:
    """Assemble the result, echoing frozen evidence verbatim (A.2)."""
    return EvaluationResult(
        outcome=outcome,
        risk_level=risk_level,
        blast_radius=copy.deepcopy(evidence.get("blast_radius") or {}),
        reason=reason,
        policy_hash=policy.policy_hash,
        ttl_seconds=ttl_seconds,
        autonomy_level=autonomy_level,
        evidence=copy.deepcopy(evidence),
    )


def _autonomy_level(autonomy_state: Optional[AutonomyState]) -> AutonomyLevel:
    """Read the autonomy level, defaulting to the least freedom granted.

    An absent state is ``SHADOW``: every agent/action class starts in SHADOW and
    trust is earned from recorded evidence, never granted by default (B6). An
    unrecognised stored mode is treated the same way rather than trusted.
    """
    if autonomy_state is None:
        return AutonomyLevel.SHADOW
    try:
        return AutonomyLevel(autonomy_state.mode)
    except ValueError:
        return AutonomyLevel.SHADOW


def _collect_strings(value: Any, depth: int = 0) -> List[str]:
    """Every string reachable inside ``value``, in a fixed order.

    Recurses through dicts and sequences because a shell command commonly
    arrives as a list of argv (``["rm", "-rf", "/srv"]``); scanning only
    top-level scalars would miss the exact case this scan exists for. Dict keys
    are sorted so the traversal -- and therefore the recorded reason -- cannot
    depend on insertion order, which is what keeps replay consistent (A.2).
    Sequence order is preserved because it is meaningful and already
    deterministic.

    ``_MAX_PARAMETER_DEPTH`` bounds the walk: ``parameters`` is typed ``Any``, so
    an arbitrarily nested or self-referential structure must not turn a deny
    check into unbounded recursion. Exceeding the bound denies the request,
    since the engine cannot inspect every value reliably.
    """
    if isinstance(value, str):
        return [value]
    if depth >= _MAX_PARAMETER_DEPTH:
        raise PolicyInterpretationError("parameters exceed the supported scan depth")
    if isinstance(value, dict):
        found: List[str] = []
        for key in sorted(value, key=str):
            found.extend(_collect_strings(value[key], depth + 1))
        return found
    if isinstance(value, (list, tuple)):
        found = []
        tokens: List[str] = []
        for item in value:
            found.extend(_collect_strings(item, depth + 1))
            if isinstance(item, str):
                tokens.append(item)
        if len(tokens) > 1:
            # An argv-style list splits a command across elements, so no single
            # token matches a whole-command pattern. Joining re-forms it.
            # Additive and deny-only: it can add a candidate that denies, never
            # remove one that would.
            found.append(" ".join(tokens))
        return found
    return []


def _match_candidates(request: ActionRequest) -> List[str]:
    """Strings the artifact's deny patterns are tested against, in fixed order.

    The target resource plus every string anywhere in ``parameters``, nested
    values included. The surface is deliberately broad: deny rules are the only
    pattern mechanism here, so over-matching can only narrow a decision (a
    denial, carrying a human-readable reason) and never widen one. The
    trade-off is false positives -- prose in a ``notes`` or ``description``
    parameter can trip a path-shaped pattern. Narrowing to "command-like"
    fields would require either a key allowlist in code (semantics outside
    ``policy_hash``, which this module does not do) or a new schema key (a
    grammar change, per C.8), so it is rejected for the same reason the
    threshold rule is.

    Only strings are inspected, at any depth: a deny rule is a content match,
    and nothing here interprets or coerces a value.
    """
    candidates = [request.target_resource]
    candidates.extend(_collect_strings(request.parameters))
    return candidates


def _first_deny_match(
    deny_rules: List[Dict[str, Any]], candidates: List[str]
) -> Optional[str]:
    """Return the reason of the first matching deny rule, else ``None``.

    Rules are evaluated in artifact order. The artifact has no allow-patterns,
    so precedence cannot change the outcome here -- only which reason is
    recorded (C.8).

    A rule the engine cannot faithfully read raises
    :class:`PolicyInterpretationError` instead of being skipped. The loader
    already rejects such artifacts (step 2), so this branch "cannot happen" --
    which is exactly why it must deny if it ever does. Skipping an uncompilable
    pattern would mean the rule silently does not fire, i.e. a deny rule quietly
    becomes an allow.
    """
    for rule in deny_rules:
        if not isinstance(rule, dict):
            raise PolicyInterpretationError(
                f"regex_deny_rules entry is {type(rule).__name__}, not an object"
            )
        missing = [field for field in DENY_RULE_FIELDS if field not in rule]
        if missing:
            raise PolicyInterpretationError(
                f"regex_deny_rules entry is missing {', '.join(missing)}"
            )
        pattern = rule["pattern"]
        if not isinstance(pattern, str):
            raise PolicyInterpretationError(
                f"regex_deny_rules pattern is {type(pattern).__name__}, not a string"
            )
        if not isinstance(rule["reason"], str):
            raise PolicyInterpretationError(
                f"regex_deny_rules reason is {type(rule['reason']).__name__}, "
                "not a string"
            )
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise PolicyInterpretationError(
                f"regex_deny_rules pattern {pattern!r} does not compile: {exc}"
            ) from exc
        for candidate in candidates:
            if compiled.search(candidate):
                return rule["reason"]
    return None


def evaluate(
    request: ActionRequest,
    policy: Policy,
    evidence: Optional[Dict[str, Any]] = None,
    autonomy_state: Optional[AutonomyState] = None,
) -> EvaluationResult:
    """Evaluate ``request`` against ``policy``; never touches the outside world."""
    frozen = evidence if isinstance(evidence, dict) else {}

    try:
        return _evaluate(request, policy, frozen, autonomy_state)
    except PolicyInterpretationError as exc:
        return _deny(request, policy, frozen, f"policy is uninterpretable: {exc}")
    except Exception as exc:  # noqa: BLE001 - fail closed (see module docstring)
        return _deny(
            request,
            policy,
            frozen,
            f"evaluation failed closed: {type(exc).__name__}",
        )


def _evaluate(
    request: ActionRequest,
    policy: Policy,
    evidence: Dict[str, Any],
    autonomy_state: Optional[AutonomyState],
) -> EvaluationResult:
    rules = policy.rules_json or {}

    sandbox_root = rules.get("sandbox_root")
    risk_map = rules.get("risk_map")
    role_permissions = rules.get("role_permissions")
    environment_rules = rules.get("environment_rules")
    deny_rules = rules.get("regex_deny_rules")
    ttl_map = rules.get("ttl_map")

    autonomy_level = _autonomy_level(autonomy_state)

    if not sandbox_root or not isinstance(sandbox_root, str):
        return _deny(request, policy, evidence, "policy declares no sandbox_root")
    if not isinstance(risk_map, dict) or not risk_map:
        return _deny(request, policy, evidence, "policy declares no risk_map")
    if not isinstance(role_permissions, dict) or not role_permissions:
        return _deny(
            request, policy, evidence, "policy declares no role_permissions"
        )
    if not isinstance(ttl_map, dict):
        return _deny(request, policy, evidence, "policy declares no ttl_map")
    if not isinstance(environment_rules, dict):
        return _deny(
            request, policy, evidence, "policy declares no environment_rules"
        )
    if not isinstance(deny_rules, list):
        return _deny(
            request,
            policy,
            evidence,
            "policy declares no regex_deny_rules"
            if deny_rules is None
            else "policy regex_deny_rules is malformed",
        )

    granted = role_permissions.get(request.actor_role)
    if granted is None:
        return _deny(
            request,
            policy,
            evidence,
            f"role {request.actor_role!r} is not in the policy",
        )
    if not isinstance(granted, list):
        # A non-list would make ``action_class in granted`` a substring test, so
        # "file" would match a permission string of "delete_file" and grant an
        # action the policy never granted. The loader rejects this too (C.4);
        # reaching it means the engine was handed something uninterpretable.
        return _deny(
            request,
            policy,
            evidence,
            f"policy role_permissions[{request.actor_role!r}] is "
            f"{type(granted).__name__}, not a list",
        )
    if request.action_class not in risk_map:
        return _deny(
            request,
            policy,
            evidence,
            f"action {request.action_class!r} is not in the policy risk_map",
        )
    if request.action_class not in granted:
        return _deny(
            request,
            policy,
            evidence,
            f"role {request.actor_role!r} may not perform "
            f"{request.action_class!r}",
        )

    raw_risk = risk_map.get(request.action_class)
    try:
        risk_level = RiskLevel(raw_risk)
    except ValueError:
        return _deny(
            request,
            policy,
            evidence,
            f"policy risk_map has an invalid level for "
            f"{request.action_class!r}",
            autonomy_level=autonomy_level,
        )

    resolved_target = to_absolute(sandbox_root, request.target_resource)
    if resolved_target is None or not is_within_sandbox(
        resolved_target, sandbox_root
    ):
        return _deny(
            request,
            policy,
            evidence,
            f"target {request.target_resource!r} is outside the policy sandbox",
            risk_level=risk_level,
            autonomy_level=autonomy_level,
        )

    matched_reason = _first_deny_match(deny_rules, _match_candidates(request))
    if matched_reason is not None:
        return _deny(
            request,
            policy,
            evidence,
            f"denied by policy rule: {matched_reason}",
            risk_level=risk_level,
            autonomy_level=autonomy_level,
        )

    if request.action_class == "move_file":
        destination = request.parameters.get("destination")
        if not isinstance(destination, str) or not destination:
            return _deny(request, policy, evidence, "move_file requires destination", risk_level, autonomy_level)
        absolute_destination = to_absolute(sandbox_root, destination)
        if absolute_destination is None or not is_within_sandbox(absolute_destination, sandbox_root):
            return _deny(request, policy, evidence, "move destination is outside the sandbox", risk_level, autonomy_level)

    approval = evidence.get("review_approval") or {}
    approved = (
        approval.get("approved") is True
        and isinstance(approval.get("reviewer_id"), str)
        and bool(approval["reviewer_id"].strip())
        and bool(approval.get("original_decision_id"))
        and approval.get("action_fingerprint") == fingerprint(request)
    )

    prod_escalates = environment_rules.get("prod_escalates") or []
    destructive_allowed = (
        environment_rules.get("destructive_allowed_environments") or []
    )
    if (
        request.action_class in prod_escalates
        and request.environment not in destructive_allowed
        and not approved
    ):
        return _result(
            request,
            policy,
            evidence,
            DecisionOutcome.ESCALATED,
            risk_level,
            f"{request.action_class!r} escalates in environment "
            f"{request.environment!r}",
            None,
            autonomy_level,
        )

    ttl = ttl_map.get(risk_level.value)
    if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl <= 0:
        # null *or* non-positive means no usable token. A ttl of 0 or -1 would
        # reach ALLOWED carrying a token that is already expired, which C.4
        # forbids -- the outcome would be wrong even though such a token grants
        # nothing, so this is a correctness guard rather than a safety one.
        return _result(
            request,
            policy,
            evidence,
            DecisionOutcome.ESCALATED,
            risk_level,
            f"ttl_map declares no issuable token for {risk_level.value}; "
            f"escalating rather than minting a born-expired token",
            None,
            autonomy_level,
        )

    if evidence.get("semantic_flag") is True and not approved:
        return _result(
            request, policy, evidence, DecisionOutcome.ESCALATED, risk_level,
            f"semantic advisory requires review: {evidence.get('semantic_warning') or 'flagged similarity'}",
            None, autonomy_level,
        )

    blast = evidence.get("blast_radius") or {}
    if blast.get("requires_review") is True and not approved:
        return _result(
            request, policy, evidence, DecisionOutcome.ESCALATED, risk_level,
            f"blast radius requires review: {blast.get('warning') or 'broad resource scope'}",
            None, autonomy_level,
        )

    if autonomy_level is AutonomyLevel.SHADOW:
        return _result(
            request,
            policy,
            evidence,
            DecisionOutcome.SHADOW_LOGGED,
            risk_level,
            "agent autonomy is SHADOW; decision recorded, not executed",
            None,
            autonomy_level,
        )
    if autonomy_level is AutonomyLevel.ASSISTED and not approved:
        return _result(
            request,
            policy,
            evidence,
            DecisionOutcome.PENDING_APPROVAL,
            risk_level,
            "agent autonomy is ASSISTED; awaiting human sign-off",
            None,
            autonomy_level,
        )

    return _result(
        request,
        policy,
        evidence,
        DecisionOutcome.ALLOWED,
        risk_level,
        f"permitted by policy as {risk_level.value}",
        ttl,
        autonomy_level,
    )
