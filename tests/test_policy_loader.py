"""Validation and storage behavior for policy artifacts."""

import copy
import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlmodel import Session, SQLModel, create_engine

from src.models.policy import Policy
from src.services.policy_loader import (
    PolicyValidationError,
    canonical_policy_hash,
    load_policy_file,
    seed_policy_artifacts,
    validate_policy,
)

POLICY_DIR = Path(__file__).resolve().parents[1] / "policies"


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine, tables=[Policy.__table__])
    with Session(engine) as db:
        yield db


def valid_rules():
    return json.loads((POLICY_DIR / "policy_v1.0.0.json").read_text())


def test_shipped_artifacts_validate_and_hash_stably():
    rules, digest = load_policy_file(POLICY_DIR / "policy_v1.0.0.json")
    assert rules["version"] == "1.0.0"
    assert digest == canonical_policy_hash(rules)
    assert digest == canonical_policy_hash(copy.deepcopy(rules))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.pop("ttl_map"),
        lambda r: r.update(unrecognized=True),
        lambda r: r.update(sandbox_root="relative/sandbox"),
        lambda r: r["risk_map"].update(read_file="EXTREME"),
        lambda r: r["role_permissions"]["workspace_agent"].append("unknown_action"),
        lambda r: r["regex_deny_rules"].append({"pattern": "[", "reason": "bad"}),
        lambda r: r["regex_deny_rules"].append(
            {"pattern": "(a+)+$", "reason": "catastrophic backtracking"}
        ),
        lambda r: r["ttl_map"].update(LOW=0),
    ],
)
def test_invalid_artifacts_are_rejected(mutate):
    rules = valid_rules()
    mutate(rules)
    with pytest.raises(PolicyValidationError):
        validate_policy(rules)


def test_seed_bootstraps_active_and_candidate_and_is_idempotent(session):
    first = seed_policy_artifacts(session, POLICY_DIR)
    session.commit()
    assert [(p.version, p.is_active, p.is_shadow) for p in first] == [
        ("1.0.0", True, False),
        ("1.1.0", False, True),
    ]

    again = seed_policy_artifacts(session, POLICY_DIR)
    session.commit()
    assert len(again) == 2
    assert len(session.exec(select(Policy)).all()) == 2


def test_all_files_validate_before_any_policy_is_added(session, tmp_path):
    (tmp_path / "policy_v1.0.0.json").write_text(json.dumps(valid_rules()))
    invalid = valid_rules()
    invalid["sandbox_root"] = "relative"
    invalid["version"] = "1.1.0"
    (tmp_path / "policy_v1.1.0.json").write_text(json.dumps(invalid))

    with pytest.raises(PolicyValidationError):
        seed_policy_artifacts(session, tmp_path)
    assert session.exec(select(Policy)).all() == []
