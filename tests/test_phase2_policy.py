"""Phase 2 policy engine and policy artifacts (C.4, C.8, A2).

Three groups:

1. **Artifact integrity** -- the shipped policies parse, declare an absolute
   ``sandbox_root``, satisfy the closed schema, and differ only where intended.
2. **Review fixes** -- one test per finding from the engine review, so a
   regression re-opens a specific decision rather than "tests are red."
3. **Engine contract** -- precedence, autonomy gating, evidence echo, and
   determinism (the A2 replay precondition).

``evaluate`` is pure, so no fixtures need a database: a ``Policy`` row is a
plain SQLModel instance and can be constructed in memory.
"""

import copy
import json
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from src.models.autonomy import AutonomyState
from src.models.policy import Policy
from src.models.request import (
    ActionRequest,
    AutonomyLevel,
    DecisionOutcome,
    RiskLevel,
)
from src.services.policy_engine import evaluate

POLICY_DIR = Path(__file__).resolve().parent.parent / "policies"
SANDBOX = "/srv/agent/workspace"
IN_SANDBOX = f"{SANDBOX}/notes.txt"

# The full closed schema (C.4). Anything outside this set is a policy change
# that has to be argued for, not slipped in.
REQUIRED_KEYS = {
    "version",
    "sandbox_root",
    "risk_map",
    "role_permissions",
    "environment_rules",
    "regex_deny_rules",
    "ttl_map",
}


def load_artifact(name: str) -> Dict[str, Any]:
    return json.loads((POLICY_DIR / name).read_text())


def make_policy(rules: Dict[str, Any], policy_hash: str = "test-hash") -> Policy:
    """An in-memory ``Policy`` row. No session: the engine only reads fields."""
    return Policy(
        version=rules.get("version", "test"),
        policy_hash=policy_hash,
        rules_json=rules,
        is_active=True,
    )


def make_request(**overrides: Any) -> ActionRequest:
    payload: Dict[str, Any] = {
        "agent_id": "agent-1",
        "actor_role": "workspace_agent",
        "action_class": "read_file",
        "target_resource": IN_SANDBOX,
        "parameters": {},
        "environment": "development",
    }
    payload.update(overrides)
    return ActionRequest(**payload)


def autonomy(mode: str) -> AutonomyState:
    return AutonomyState(
        agent_id="agent-1", action_class="read_file", mode=mode, streak=5
    )


def rules_with(**overrides: Any) -> Dict[str, Any]:
    """A valid v1.1 rule set with selected keys replaced or removed.

    ``None`` deletes a key, which is how the "missing required key" cases are
    expressed without duplicating the whole artifact.
    """
    rules = copy.deepcopy(load_artifact("policy_v1.1.0.json"))
    for key, value in overrides.items():
        if value is None:
            rules.pop(key, None)
        else:
            rules[key] = value
    return rules


# --------------------------------------------------------------------------
# 1. Artifact integrity
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["policy_v1.0.0.json", "policy_v1.1.0.json"])
def test_shipped_policy_parses_and_declares_absolute_root(name: str) -> None:
    rules = load_artifact(name)
    assert set(rules) == REQUIRED_KEYS
    assert rules["sandbox_root"] == SANDBOX
    assert rules["sandbox_root"].startswith("/")


@pytest.mark.parametrize("name", ["policy_v1.0.0.json", "policy_v1.1.0.json"])
def test_shipped_policy_values_are_all_valid(name: str) -> None:
    """The loader's checks, re-stated here so the artifacts are verified
    independently of the loader's existence (step 2 is not built yet)."""
    import re

    rules = load_artifact(name)
    for action, level in rules["risk_map"].items():
        assert level in {r.value for r in RiskLevel}, f"{action} -> {level}"
    for role, actions in rules["role_permissions"].items():
        assert isinstance(actions, list)
        for action in actions:
            assert action in rules["risk_map"], f"{role} names unknown {action}"
    for level in rules["risk_map"].values():
        assert level in rules["ttl_map"], f"{level} has no ttl_map entry"
    for rule in rules["regex_deny_rules"]:
        assert set(rule) == {"pattern", "reason"}
        re.compile(rule["pattern"])


def test_v1_1_differes_from_v1_0_only_by_version_and_deny_rules() -> None:
    """Shadow comparison must isolate the deny-rule change. Sharing
    ``sandbox_root`` is what makes that comparison meaningful -- a root change
    would mix two variables into one diff (C.4)."""
    v10 = load_artifact("policy_v1.0.0.json")
    v11 = load_artifact("policy_v1.1.0.json")

    assert v10["sandbox_root"] == v11["sandbox_root"]
    assert set(v10) - {"version", "regex_deny_rules"} == set(v11) - {
        "version",
        "regex_deny_rules",
    }
    for key in REQUIRED_KEYS - {"version", "regex_deny_rules"}:
        assert v10[key] == v11[key], f"{key} drifted between versions"

    extra = len(v11["regex_deny_rules"]) - len(v10["regex_deny_rules"])
    assert extra == 2, "v1.1 adds exactly the .env and .git deny rules"


def test_v1_1_actually_tightens_the_decision() -> None:
    """A version that changes only bookkeeping would pass the test above. This
    confirms the new rules change an outcome, and only for the paths meant."""
    v11 = make_policy(load_artifact("policy_v1.1.0.json"))

    env_request = make_request(target_resource=f"{SANDBOX}/.env")
    assert evaluate(env_request, v11, autonomy_state=autonomy("LIVE")).outcome == (
        DecisionOutcome.DENIED
    )

    git_request = make_request(target_resource=f"{SANDBOX}/.git/config")
    assert evaluate(git_request, v11, autonomy_state=autonomy("LIVE")).outcome == (
        DecisionOutcome.DENIED
    )

    # A path that merely *contains* ".env" as a substring is not an env file.
    ordinary = make_request(target_resource=f"{SANDBOX}/.environment-notes")
    assert evaluate(ordinary, v11, autonomy_state=autonomy("LIVE")).outcome == (
        DecisionOutcome.ALLOWED
    )


# --------------------------------------------------------------------------
# 2. Review fixes
# --------------------------------------------------------------------------


def test_uncompilable_deny_pattern_denies_rather_than_being_skipped() -> None:
    """Finding 1. A deny rule that will not compile must not silently stop
    firing: skipping it turns the rule into an allow. The loader rejects such
    patterns, so this asserts the engine's defence in depth."""
    rules = rules_with(
        regex_deny_rules=[
            {"pattern": "(?i)(unclosed", "reason": "broken pattern"},
            {"pattern": ".*", "reason": "would allow if reached"},
        ]
    )
    result = evaluate(
        make_request(), make_policy(rules), autonomy_state=autonomy("LIVE")
    )
    assert result.outcome == DecisionOutcome.DENIED
    assert "uninterpretable" in result.reason


def test_deny_rule_that_is_not_an_object_denies() -> None:
    """Finding 1, sibling case: a bare string in the rule list used to be
    skipped via ``rule.get`` blowing up on a non-dict."""
    rules = rules_with(regex_deny_rules=["rm -rf /"])
    result = evaluate(
        make_request(), make_policy(rules), autonomy_state=autonomy("LIVE")
    )
    assert result.outcome == DecisionOutcome.DENIED
    assert "uninterpretable" in result.reason


@pytest.mark.parametrize(
    "bad_rule",
    [
        {"reason": "no pattern field"},
        {"pattern": "x"},
        {"pattern": 42, "reason": "pattern is not a string"},
        {"pattern": "x", "reason": None},
    ],
)
def test_malformed_deny_rule_fields_deny(bad_rule: Dict[str, Any]) -> None:
    rules = rules_with(regex_deny_rules=[bad_rule])
    result = evaluate(
        make_request(), make_policy(rules), autonomy_state=autonomy("LIVE")
    )
    assert result.outcome == DecisionOutcome.DENIED
    assert "uninterpretable" in result.reason


def test_missing_regex_deny_rules_denies() -> None:
    """The loader requires the key; absence means the engine was handed
    something it cannot interpret, so it denies rather than assuming no rules."""
    rules = rules_with(regex_deny_rules=None)
    result = evaluate(
        make_request(), make_policy(rules), autonomy_state=autonomy("LIVE")
    )
    assert result.outcome == DecisionOutcome.DENIED
    assert "regex_deny_rules" in result.reason


def test_non_list_permissions_do_not_grant_via_substring_match() -> None:
    """Finding 4. With a string permission value, ``"read_file" in "delete_file"``
    is a substring test, so an unintended grant is possible. Must deny."""
    rules = rules_with(
        role_permissions={
            "read_only_agent": "delete_file read_file",
            "workspace_agent": ["read_file"],
            "admin_agent": ["read_file"],
        }
    )
    result = evaluate(
        make_request(actor_role="read_only_agent"),
        make_policy(rules),
        autonomy_state=autonomy("LIVE"),
    )
    assert result.outcome == DecisionOutcome.DENIED
    assert "not a list" in result.reason


@pytest.mark.parametrize("ttl", [0, -1])
def test_non_positive_ttl_escalates_instead_of_minting_an_expired_token(
    ttl: int,
) -> None:
    """Finding 6. C.4: a null TTL means escalate. A zero or negative TTL is the
    same situation by another name -- the token would be born expired."""
    rules = rules_with(ttl_map={**rules_with()["ttl_map"], "READ_ONLY": ttl})
    result = evaluate(
        make_request(), make_policy(rules), autonomy_state=autonomy("LIVE")
    )
    assert result.outcome == DecisionOutcome.ESCALATED
    assert result.ttl_seconds is None


def test_null_ttl_escalates() -> None:
    """The documented CRITICAL case: no issuable token, so escalate."""
    rules = rules_with()
    result = evaluate(
        make_request(
            actor_role="admin_agent",
            action_class="delete_directory",
            environment="development",
        ),
        make_policy(rules),
        autonomy_state=autonomy("LIVE"),
    )
    assert result.outcome == DecisionOutcome.ESCALATED
    assert result.ttl_seconds is None
    assert "born-expired" in result.reason


def test_non_dict_evidence_fails_closed() -> None:
    """Finding 7. ``evidence`` is typed ``Optional[Dict]``, so a correct caller
    never hits this -- but the fail-closed handler must not re-raise on the same
    bad value that caused the failure."""
    result = evaluate(
        make_request(),
        make_policy(rules_with()),
        evidence="not-a-dict",  # type: ignore[arg-type]
        autonomy_state=autonomy("LIVE"),
    )
    assert result.outcome in {
        DecisionOutcome.DENIED,
        DecisionOutcome.ALLOWED,
        DecisionOutcome.ESCALATED,
    }
    assert result.evidence == {}
    assert result.blast_radius == {}


def test_nested_deny_pattern_is_scanned() -> None:
    """Finding 8. A shell command commonly arrives as argv, so a nested string
    must be scanned -- otherwise the argv case the scan exists for is missed."""
    result = evaluate(
        make_request(parameters={"command": ["rm", "-rf", "/srv/agent"]}),
        make_policy(rules_with()),
        autonomy_state=autonomy("LIVE"),
    )
    assert result.outcome == DecisionOutcome.DENIED
    assert "destructive shell payload" in result.reason


def test_deeply_nested_deny_pattern_is_scanned() -> None:
    nested: Dict[str, Any] = {"outer": {"inner": {"deeper": "rm -rf /srv"}}}
    result = evaluate(
        make_request(parameters=nested),
        make_policy(rules_with()),
        autonomy_state=autonomy("LIVE"),
    )
    assert result.outcome == DecisionOutcome.DENIED


def test_deny_scan_ignores_non_strings() -> None:
    """Numbers and booleans are not content matches; nothing coerces them."""
    result = evaluate(
        make_request(parameters={"count": 42, "force": True, "path": None}),
        make_policy(rules_with()),
        autonomy_state=autonomy("LIVE"),
    )
    assert result.outcome == DecisionOutcome.ALLOWED


# --------------------------------------------------------------------------
# 3. Engine contract
# --------------------------------------------------------------------------


def test_allowed_decision_carries_ttl_and_no_token_is_minted() -> None:
    """C.6: the engine decides the TTL, the issuer mints. Only ALLOWED carries
    a TTL, so no caller can issue a token for a denied or escalated decision."""
    result = evaluate(
        make_request(), make_policy(rules_with()), autonomy_state=autonomy("LIVE")
    )
    assert result.outcome == DecisionOutcome.ALLOWED
    assert result.ttl_seconds == 600
    assert result.risk_level == RiskLevel.READ_ONLY


@pytest.mark.parametrize(
    "mode,expected",
    [
        (AutonomyLevel.SHADOW, DecisionOutcome.SHADOW_LOGGED),
        (AutonomyLevel.ASSISTED, DecisionOutcome.PENDING_APPROVAL),
        (AutonomyLevel.LIVE, DecisionOutcome.ALLOWED),
    ],
)
def test_autonomy_gate_subdivides_a_permitted_action(
    mode: AutonomyLevel, expected: DecisionOutcome
) -> None:
    result = evaluate(
        make_request(), make_policy(rules_with()), autonomy_state=autonomy(mode)
    )
    assert result.outcome == expected


@pytest.mark.parametrize("ttl_expected", [None])
def test_shadow_and_assisted_never_carry_a_ttl(ttl_expected: Optional[int]) -> None:
    """A held or logged decision must not be executable downstream."""
    for mode in (AutonomyLevel.SHADOW, AutonomyLevel.ASSISTED):
        result = evaluate(
            make_request(),
            make_policy(rules_with()),
            autonomy_state=autonomy(mode),
        )
        assert result.ttl_seconds is ttl_expected


def test_absent_autonomy_state_defaults_to_shadow() -> None:
    """B6: trust is earned from evidence, never granted by default."""
    result = evaluate(make_request(), make_policy(rules_with()))
    assert result.outcome == DecisionOutcome.SHADOW_LOGGED
    assert result.autonomy_level == AutonomyLevel.SHADOW


def test_unrecognised_autonomy_mode_is_not_trusted() -> None:
    result = evaluate(
        make_request(), make_policy(rules_with()), autonomy_state=autonomy("SUPER")
    )
    assert result.outcome == DecisionOutcome.SHADOW_LOGGED
    assert result.autonomy_level == AutonomyLevel.SHADOW


def test_destructive_action_escalates_in_production() -> None:
    result = evaluate(
        make_request(
            actor_role="admin_agent",
            action_class="delete_file",
            environment="production",
        ),
        make_policy(rules_with()),
        autonomy_state=autonomy("LIVE"),
    )
    assert result.outcome == DecisionOutcome.ESCALATED
    assert result.ttl_seconds is None


def test_role_not_in_policy_denies() -> None:
    result = evaluate(
        make_request(actor_role="root"),
        make_policy(rules_with()),
        autonomy_state=autonomy("LIVE"),
    )
    assert result.outcome == DecisionOutcome.DENIED


def test_action_not_granted_to_role_denies() -> None:
    result = evaluate(
        make_request(actor_role="read_only_agent", action_class="delete_file"),
        make_policy(rules_with()),
        autonomy_state=autonomy("LIVE"),
    )
    assert result.outcome == DecisionOutcome.DENIED
    assert "may not perform" in result.reason


@pytest.mark.parametrize(
    "target",
    [
        "/etc/passwd",
        f"{SANDBOX}-evil/notes.txt",
        f"{SANDBOX}/../../etc/passwd",
        "../../etc/passwd",
        f"{SANDBOX}/notes/../../../etc/shadow",
    ],
)
def test_target_outside_sandbox_denies(target: str) -> None:
    result = evaluate(
        make_request(target_resource=target),
        make_policy(rules_with()),
        autonomy_state=autonomy("LIVE"),
    )
    assert result.outcome == DecisionOutcome.DENIED


def test_relative_target_is_anchored_to_the_policy_root() -> None:
    """A relative target resolves against the policy root, never the cwd, so
    the verdict cannot depend on where the process was started."""
    result = evaluate(
        make_request(target_resource="notes.txt"),
        make_policy(rules_with()),
        autonomy_state=autonomy("LIVE"),
    )
    assert result.outcome == DecisionOutcome.ALLOWED


@pytest.mark.parametrize(
    "key", ["sandbox_root", "risk_map", "role_permissions", "ttl_map", "environment_rules"]
)
def test_missing_required_key_denies(key: str) -> None:
    result = evaluate(
        make_request(), make_policy(rules_with(**{key: None})), autonomy_state=autonomy("LIVE")
    )
    assert result.outcome == DecisionOutcome.DENIED
    assert key in result.reason


def test_deny_precedes_autonomy_grant() -> None:
    """Autonomy never grants what policy forbids: a denied action stays denied
    in LIVE, and the recorded reason is the rule, not the autonomy mode."""
    result = evaluate(
        make_request(target_resource=f"{SANDBOX}/.git/HEAD"),
        make_policy(rules_with()),
        autonomy_state=autonomy("LIVE"),
    )
    assert result.outcome == DecisionOutcome.DENIED
    assert "denied by policy rule" in result.reason


def test_deny_precedes_environment_escalation() -> None:
    """Precedence order: content is checked before environment, so the reason
    is the deny rule rather than the prod escalation."""
    result = evaluate(
        make_request(
            actor_role="admin_agent",
            action_class="delete_file",
            environment="production",
            target_resource=f"{SANDBOX}/.env",
        ),
        make_policy(rules_with()),
        autonomy_state=autonomy("LIVE"),
    )
    assert result.outcome == DecisionOutcome.DENIED
    assert "denied by policy rule" in result.reason


def test_first_matching_rule_in_artifact_order_is_recorded() -> None:
    """No allow-patterns exist, so rule order cannot change the outcome -- only
    which reason is recorded, which replay depends on (A2)."""
    rules = rules_with(
        regex_deny_rules=[
            {"pattern": "notes", "reason": "first"},
            {"pattern": "notes", "reason": "second"},
        ]
    )
    result = evaluate(
        make_request(), make_policy(rules), autonomy_state=autonomy("LIVE")
    )
    assert "first" in result.reason
    assert "second" not in result.reason


def test_evidence_and_blast_radius_are_echoed_verbatim() -> None:
    """A.2: echoed, never recomputed. A deep copy, so mutating the caller's
    bundle afterwards cannot alter an already-returned result."""
    evidence = {
        "blast_radius": {"file_count": 3, "nested": {"bytes": 100}},
        "signal": "destructive",
    }
    original = copy.deepcopy(evidence)

    result = evaluate(
        make_request(), make_policy(rules_with()), evidence=evidence
    )

    assert result.evidence == original
    assert result.blast_radius == original["blast_radius"]

    evidence["blast_radius"]["file_count"] = 999
    evidence["nested"] = "mutated"
    assert result.blast_radius == original["blast_radius"]
    assert result.evidence["blast_radius"]["file_count"] == 3


def test_evaluation_is_deterministic_across_repeated_runs() -> None:
    """The A2 replay precondition: same inputs, identical result -- no clock,
    no randomness, no dict-ordering dependence."""
    policy = make_policy(rules_with())
    evidence = {"blast_radius": {"file_count": 2}, "signal": "read"}

    results = [
        evaluate(
            make_request(parameters={"command": "cat notes.txt", "flag": "-v"}),
            policy,
            evidence=evidence,
            autonomy_state=autonomy("LIVE"),
        )
        for _ in range(5)
    ]

    first = results[0].model_dump()
    for result in results[1:]:
        assert result.model_dump() == first


def test_parameter_insertion_order_does_not_change_the_reason() -> None:
    """Deny candidates are collected in sorted key order, so two payloads with
    the same content in different insertion order record the same reason."""
    forward = {"alpha": "safe", "beta": "safe", "gamma": "safe"}
    reverse = {"gamma": "safe", "beta": "safe", "alpha": "safe"}
    policy = make_policy(rules_with())

    first = evaluate(
        make_request(parameters=forward), policy, autonomy_state=autonomy("LIVE")
    )
    second = evaluate(
        make_request(parameters=reverse), policy, autonomy_state=autonomy("LIVE")
    )

    assert first.reason == second.reason


def test_policy_hash_is_carried_into_the_result() -> None:
    """The result names the artifact it was decided against, so a decision can
    be traced to an exact policy version on replay."""
    result = evaluate(
        make_request(),
        make_policy(rules_with(), policy_hash="abc123"),
        autonomy_state=autonomy("LIVE"),
    )
    assert result.policy_hash == "abc123"
