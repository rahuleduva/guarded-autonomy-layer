"""Phase 1 sandbox containment (A1).

All paths are built from pytest's ``tmp_path`` so every case is hermetic and
absolute. The guard is contractually absolute-only: a relative input denies
rather than resolving against the process working directory, so these results
must be identical no matter where pytest is invoked from.
"""

import json
import os
import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

from src.models.decision import DecisionRecord
from src.models.request import (
    ActionRequest,
    AutonomyLevel,
    DecisionOutcome,
    EvaluationResult,
    RiskLevel,
)
from src.services.decision_ledger import log_decision
from src.services.path_guard import is_within_sandbox, to_absolute


def _verdict(target: str, sandbox_root: str) -> bool:
    """Call the guard and fail loudly on any raise or non-bool return.

    The contract is that the guard only ever returns True/False. Letting an
    exception escape already fails the test; asserting the type as well pins
    the return value so a truthy non-bool cannot pass unnoticed.
    """
    result = is_within_sandbox(target, sandbox_root)
    assert isinstance(result, bool)
    return result


@pytest.fixture
def root(tmp_path):
    """An absolute sandbox root that exists, distinct from its lookalike."""
    sandbox = tmp_path / "workspace"
    sandbox.mkdir()
    return sandbox


def test_inside_root_is_allowed(root):
    assert _verdict(str(root / "a.txt"), str(root)) is True


def test_nested_path_inside_root_is_allowed(root):
    nested = root / "deep" / "nested" / "a.txt"
    nested.parent.mkdir(parents=True)
    assert _verdict(str(nested), str(root)) is True


def test_root_itself_is_allowed(root):
    assert _verdict(str(root), str(root)) is True


def test_prefix_lookalike_is_rejected(root):
    lookalike = root.parent / (root.name + "-evil") / "a.txt"
    assert _verdict(str(lookalike), str(root)) is False


def test_traversal_is_rejected(root):
    # Path does not normalise "..", so the literal segment reaches realpath.
    assert _verdict(str(root / ".." / "etc" / "passwd"), str(root)) is False


def test_absolute_path_outside_root_is_rejected(root):
    assert _verdict("/etc/passwd", str(root)) is False


def test_relative_target_is_rejected(root):
    assert _verdict("workspace/a.txt", str(root)) is False


def test_relative_sandbox_root_is_rejected(root):
    assert _verdict(str(root / "a.txt"), "workspace") is False


def test_symlink_case_is_deliberately_out_of_scope(root):
    """Documented limitation (Phase 11): the guard is pure string algebra.

    The symlink still resolves to /etc for the OS, but the guard never asks
    the filesystem, so it cannot see that. Recorded rather than fixed --
    A1's core is the prefix bug, and closing this would reintroduce
    realpath and re-couple the checker to the machine.
    """
    link = root / "link"
    os.symlink("/etc", link)
    # Explicitly not asserted as False: detecting this is out of scope.
    assert is_within_sandbox(str(link / "passwd"), str(root)) is True


def test_verdict_is_independent_of_the_filesystem(tmp_path):
    """A root that does not exist evaluates identically to one that does.

    Proof that the guard performs no filesystem access: existence cannot
    change the answer because existence is never consulted.
    """
    missing = tmp_path / "no-such-directory"
    assert _verdict(str(missing / "a.txt"), str(missing)) is True
    assert _verdict("/etc/passwd", str(missing)) is False


def test_relative_root_never_consults_cwd(root, monkeypatch, tmp_path):
    """The cwd must not influence the verdict (A1)."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert _verdict(str(root / "a.txt"), "workspace") is False
    assert _verdict("workspace/a.txt", str(root)) is False


class TestToAbsolute:
    def test_relative_target_is_anchored_to_base(self, root):
        assert to_absolute(str(root), "a.txt") == str(root / "a.txt")

    def test_nested_relative_target_is_anchored(self, root):
        assert to_absolute(str(root), "deep/a.txt") == str(root / "deep" / "a.txt")

    def test_absolute_target_is_returned_unchanged(self, root):
        absolute = str(root / "a.txt")
        assert to_absolute(str(root), absolute) == absolute

    def test_relative_base_dir_returns_none(self):
        assert to_absolute("workspace", "a.txt") is None

    def test_traversal_collapses_to_an_escaping_absolute_path(self, root, tmp_path):
        # Anchoring is not enough: ".." must be resolved so containment sees it.
        assert to_absolute(str(root), "../etc/passwd") == str(tmp_path / "etc" / "passwd")

    def test_base_dir_is_not_resolved_against_cwd(self, root, monkeypatch, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        assert to_absolute(str(root), "a.txt") == str(root / "a.txt")


@pytest.fixture
def session():
    """In-memory ledger. Tests may build schema; the application never does."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine, tables=[DecisionRecord.__table__])
    with Session(engine) as s:
        yield s


def _request(**overrides):
    payload = {
        "agent_id": "agent-1",
        "actor_role": "developer",
        "action_class": "read_file",
        "target_resource": "/workspace/a.txt",
    }
    payload.update(overrides)
    return ActionRequest(**payload)


def _result(**overrides):
    payload = {
        "outcome": DecisionOutcome.ALLOWED,
        "risk_level": RiskLevel.READ_ONLY,
        "reason": "low risk read inside scope",
        "policy_hash": "h1",
        "autonomy_level": AutonomyLevel.ASSISTED,
        "evidence": {"blast_radius": {"files_touched": 1}},
    }
    payload.update(overrides)
    return EvaluationResult(**payload)


class TestDecisionLedger:
    """Fail-closed audit writes (A3) and full-entropy ids (A7)."""

    def test_decision_is_persisted_with_complete_request(self, session):
        request = _request()
        record = log_decision(session, request, _result())
        stored = session.get(DecisionRecord, record.decision_id)
        assert stored is not None
        assert stored.request_json["agent_id"] == "agent-1"
        assert stored.request_json["target_resource"] == "/workspace/a.txt"
        assert stored.request_json["environment"] == "production"
        assert stored.target_resource == "/workspace/a.txt"
        assert stored.actor_role == "developer"

    def test_request_json_is_json_serialisable(self, session):
        # mode="json" must render the timestamp as a string, not a datetime.
        record = log_decision(session, _request(), _result())
        assert isinstance(record.request_json["timestamp"], str)
        json.dumps(record.request_json)

    def test_enums_are_stored_as_plain_values(self, session):
        record = log_decision(session, _request(), _result())
        assert record.risk_level == "READ_ONLY"
        assert record.outcome == "ALLOWED"
        assert record.autonomy_level == "ASSISTED"

    def test_ids_are_full_uuid_v4(self, session):
        request = _request()
        record = log_decision(session, request, _result())
        for value in (record.decision_id, record.request_id):
            parsed = uuid.UUID(value)
            assert parsed.version == 4
            assert len(value) == 36

    def test_duplicate_request_id_raises_explicitly(self, session):
        request = _request()
        log_decision(session, request, _result())
        session.commit()
        with pytest.raises(IntegrityError):
            log_decision(session, request, _result())

    def test_failed_audit_write_leaves_no_record(self, session):
        request = _request()
        log_decision(session, request, _result())
        session.commit()
        with pytest.raises(IntegrityError):
            log_decision(session, request, _result())
        session.rollback()
        assert len(session.exec(select(DecisionRecord)).all()) == 1

    def test_evidence_carries_blast_radius(self, session):
        record = log_decision(session, _request(), _result())
        assert record.evidence_json["blast_radius"] == {"files_touched": 1}

    def test_optional_columns_default_to_null(self, session):
        record = log_decision(session, _request(), _result())
        assert record.capability_token_hash is None
        assert record.reviewer_id is None

    def test_token_hash_is_recorded_when_supplied(self, session):
        record = log_decision(session, _request(), _result(), capability_token_hash="tok-abc")
        assert record.capability_token_hash == "tok-abc"

    def test_caller_owns_the_commit(self, session):
        log_decision(session, _request(), _result())
        # Flushed, not committed: the row is invisible to another connection
        # until the caller commits, so the action cannot precede the ledger.
        assert len(session.exec(select(DecisionRecord)).all()) == 1
        session.rollback()
        assert len(session.exec(select(DecisionRecord)).all()) == 0
