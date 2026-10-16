"""Failed requests must leave operational evidence without a decision row."""
from fastapi.testclient import TestClient
from sqlmodel import select

from src.config import settings
from src.evaluation import demo_session
from src.main import create_app
from src.models.decision import DecisionRecord
from src.models.monitoring import RequestAttemptEvent
from src.models.clarification import RequestInterpretation
from src.services import monitoring


def test_parser_rejection_is_monitored_without_a_decision(monkeypatch):
    monkeypatch.setattr(settings, "SEMANTIC_PIPELINE_MODE", "baseline")
    with demo_session() as session:
        RequestInterpretation.__table__.create(session.bind)
        with TestClient(create_app(session.bind)) as client:
            response = client.post("/api/v1/actions/evaluate-prompt", json={
                "agent_id": "monitoring-test", "actor_role": "workspace_agent",
                "raw_prompt": "do something ambiguous",
            })
        assert response.status_code == 400
        attempt_id = response.headers["X-Correlation-ID"]
        events = session.exec(select(RequestAttemptEvent).where(
            RequestAttemptEvent.attempt_id == attempt_id)).all()
        assert [event.stage for event in events] == ["parser", "interpretation", "commit", "request"]
        assert events[-1].http_status == 400
        assert all(event.route == "/api/v1/actions/evaluate-prompt" for event in events)
        assert session.exec(select(DecisionRecord)).all() == []
        assert next(e for e in events if e.stage == 'interpretation').parser_trace['prompt'] == 'do something ambiguous'
        assert session.exec(select(RequestInterpretation)).one().status == 'rejected'


def test_successful_evaluation_records_stages_and_decision(monkeypatch):
    monkeypatch.setattr(settings, "SEMANTIC_PIPELINE_MODE", "baseline")
    with demo_session() as session:
        with TestClient(create_app(session.bind)) as client:
            response = client.post("/api/v1/actions/evaluate", json={
                "agent_id": "monitoring-test", "actor_role": "workspace_agent",
                "action_class": "read_file", "target_resource": "/srv/agent/workspace/note.txt",
                "environment": "development",
            })
        assert response.status_code == 200
        attempt_id = response.headers["X-Correlation-ID"]
        events = session.exec(select(RequestAttemptEvent).where(
            RequestAttemptEvent.attempt_id == attempt_id).order_by(RequestAttemptEvent.sequence)).all()
        stages = [event.stage for event in events]
        assert stages == ["autonomy_lookup", "blast_radius", "semantic_baseline",
                          "decision", "autonomy_record", "commit", "request"]
        assert next(event for event in events if event.stage == "decision").decision_id == response.json()["decision_id"]


def test_monitoring_database_failure_does_not_raise(caplog, monkeypatch):
    caplog.set_level("WARNING", logger="src.services.monitoring")
    monkeypatch.setattr(monitoring.logger, "disabled", False)
    def fail_session(_):
        raise OSError("synthetic database failure")
    monkeypatch.setattr(monitoring, "Session", fail_session)
    attempt, token = monitoring.begin()
    try:
        monitoring.record("parser", "failed", error_type="ValueError")
    finally:
        monitoring.end(token)
    monitoring.persist(attempt, object())
    assert "request_attempt_persist_failed" in caplog.text
    assert "request_attempt_event_fallback" in caplog.text
