"""Validate, hash, and seed versioned policy artifacts (C.4)."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from src.models.enums import RiskLevel
from src.models.policy import Policy

POLICY_DIR = Path(__file__).resolve().parents[2] / "policies"
REQUIRED_KEYS = {
    "version",
    "sandbox_root",
    "risk_map",
    "role_permissions",
    "environment_rules",
    "regex_deny_rules",
    "ttl_map",
}
ENVIRONMENT_RULE_KEYS = {"destructive_allowed_environments", "prod_escalates"}
REGEX_RULE_KEYS = {"pattern", "reason"}
MAX_REGEX_LENGTH = 256


class PolicyValidationError(ValueError):
    """A policy artifact is invalid or unsafe to interpret."""


def validate_policy(rules: Any) -> dict[str, Any]:
    """Validate and return a detached policy document, raising on any issue."""
    if not isinstance(rules, dict):
        raise PolicyValidationError("policy must be a JSON object")

    missing = REQUIRED_KEYS - rules.keys()
    unknown = rules.keys() - REQUIRED_KEYS
    if missing:
        raise PolicyValidationError(f"missing policy keys: {sorted(missing)}")
    if unknown:
        raise PolicyValidationError(f"unknown policy keys: {sorted(unknown)}")

    version = rules["version"]
    if not isinstance(version, str) or not version.strip():
        raise PolicyValidationError("version must be a non-empty string")

    sandbox_root = rules["sandbox_root"]
    if not isinstance(sandbox_root, str) or not os.path.isabs(sandbox_root):
        raise PolicyValidationError("sandbox_root must be an absolute path")

    risk_map = rules["risk_map"]
    if not isinstance(risk_map, dict) or not risk_map:
        raise PolicyValidationError("risk_map must be a non-empty object")
    for action, risk in risk_map.items():
        if not isinstance(action, str) or not action.strip():
            raise PolicyValidationError("risk_map action names must be non-empty strings")
        if not isinstance(risk, str) or risk not in {level.value for level in RiskLevel}:
            raise PolicyValidationError(f"invalid risk level for action {action!r}")

    permissions = rules["role_permissions"]
    if not isinstance(permissions, dict) or not permissions:
        raise PolicyValidationError("role_permissions must be a non-empty object")
    for role, actions in permissions.items():
        if not isinstance(role, str) or not role.strip():
            raise PolicyValidationError("role names must be non-empty strings")
        if not isinstance(actions, list) or any(not isinstance(a, str) for a in actions):
            raise PolicyValidationError(f"permissions for {role!r} must be a list of strings")
        if len(actions) != len(set(actions)):
            raise PolicyValidationError(f"permissions for {role!r} contain duplicates")
        undeclared = set(actions) - risk_map.keys()
        if undeclared:
            raise PolicyValidationError(
                f"role {role!r} names actions absent from risk_map: {sorted(undeclared)}"
            )

    environment_rules = rules["environment_rules"]
    if not isinstance(environment_rules, dict):
        raise PolicyValidationError("environment_rules must be an object")
    if environment_rules.keys() != ENVIRONMENT_RULE_KEYS:
        raise PolicyValidationError(
            "environment_rules must contain exactly "
            f"{sorted(ENVIRONMENT_RULE_KEYS)}"
        )
    for key, values in environment_rules.items():
        if not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values):
            raise PolicyValidationError(f"environment_rules.{key} must be a list of strings")
        if len(values) != len(set(values)):
            raise PolicyValidationError(f"environment_rules.{key} contains duplicates")
        if key == "prod_escalates" and set(values) - risk_map.keys():
            raise PolicyValidationError("prod_escalates contains an action absent from risk_map")

    ttl_map = rules["ttl_map"]
    expected_risks = {level.value for level in RiskLevel}
    if not isinstance(ttl_map, dict) or ttl_map.keys() != expected_risks:
        raise PolicyValidationError(f"ttl_map must contain exactly {sorted(expected_risks)}")
    for risk, ttl in ttl_map.items():
        if ttl is not None and (not isinstance(ttl, int) or isinstance(ttl, bool) or ttl <= 0):
            raise PolicyValidationError(f"ttl_map.{risk} must be a positive integer or null")

    regex_rules = rules["regex_deny_rules"]
    if not isinstance(regex_rules, list):
        raise PolicyValidationError("regex_deny_rules must be a list")
    for index, rule in enumerate(regex_rules):
        if not isinstance(rule, dict) or rule.keys() != REGEX_RULE_KEYS:
            raise PolicyValidationError(
                f"regex_deny_rules[{index}] must contain exactly pattern and reason"
            )
        pattern, reason = rule["pattern"], rule["reason"]
        if not isinstance(pattern, str) or not pattern or len(pattern) > MAX_REGEX_LENGTH:
            raise PolicyValidationError(
                f"regex_deny_rules[{index}].pattern must be 1-{MAX_REGEX_LENGTH} characters"
            )
        if not isinstance(reason, str) or not reason.strip():
            raise PolicyValidationError(f"regex_deny_rules[{index}].reason must be non-empty")
        _validate_regex(pattern, index)

    # Roundtrip gives the caller a detached, JSON-compatible document and
    # catches unsupported values before any database operation.
    try:
        return json.loads(json.dumps(rules, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise PolicyValidationError(f"policy contains non-JSON values: {exc}") from exc


def _validate_regex(pattern: str, index: int) -> None:
    """Compile a bounded deny regex and reject common catastrophic forms.

    The policy language only needs simple deny-patterns. This conservative
    check rejects nested quantified groups and backreferences, which commonly
    cause catastrophic backtracking; the length cap bounds remaining exposure.
    """
    try:
        re.compile(pattern)
    except re.error as exc:
        raise PolicyValidationError(
            f"regex_deny_rules[{index}].pattern does not compile: {exc}"
        ) from exc
    if re.search(r"\\[1-9]", pattern) or re.search(
        r"\([^)]*[+*{][^)]*\)[+*{]", pattern
    ):
        raise PolicyValidationError(
            f"regex_deny_rules[{index}].pattern uses an unsupported backtracking construct"
        )


def canonical_policy_hash(rules: dict[str, Any]) -> str:
    """Return SHA-256 of the validated policy's canonical JSON representation."""
    validated = validate_policy(rules)
    canonical = json.dumps(
        validated, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def load_policy_file(path: str | Path) -> tuple[dict[str, Any], str]:
    """Read one JSON policy file, validate it, and return rules plus hash."""
    source = Path(path)
    try:
        rules = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyValidationError(f"cannot read policy file {source}: {exc}") from exc
    validated = validate_policy(rules)
    return validated, canonical_policy_hash(validated)


def seed_policy_artifacts(
    session: Session,
    policy_dir: str | Path = POLICY_DIR,
) -> list[Policy]:
    """Validate and idempotently stage policy rows; the caller commits.

    Every JSON artifact in the directory is validated before the first write.
    On an empty table, version 1.0.0 becomes active and 1.1.0 becomes shadow.
    Existing policy state is never rewritten. Flushes expose constraint errors
    to the caller while keeping transaction ownership with that caller.
    """
    directory = Path(policy_dir)
    files = sorted(directory.glob("policy_*.json"))
    if not files:
        raise PolicyValidationError(f"no policy_*.json artifacts found in {directory}")

    validated_files: list[tuple[dict[str, Any], str]] = [load_policy_file(path) for path in files]
    versions = [rules["version"] for rules, _ in validated_files]
    if len(versions) != len(set(versions)):
        raise PolicyValidationError("policy artifacts contain duplicate versions")

    existing = session.exec(select(Policy)).all()
    bootstrap = not existing
    if bootstrap and not {"1.0.0", "1.1.0"}.issubset(set(versions)):
        raise PolicyValidationError("an empty policy table requires policy versions 1.0.0 and 1.1.0")
    existing_by_hash = {row.policy_hash: row for row in existing}
    staged: list[Policy] = []

    for rules, policy_hash in validated_files:
        if policy_hash in existing_by_hash:
            staged.append(existing_by_hash[policy_hash])
            continue

        is_active = bootstrap and rules["version"] == "1.0.0"
        is_shadow = bootstrap and rules["version"] == "1.1.0"
        row = Policy(
            version=rules["version"],
            policy_hash=policy_hash,
            rules_json=rules,
            is_active=is_active,
            is_shadow=is_shadow,
            activated_at=datetime.now(timezone.utc) if is_active else None,
        )
        session.add(row)
        staged.append(row)
        existing_by_hash[policy_hash] = row

    session.flush()
    return staged
