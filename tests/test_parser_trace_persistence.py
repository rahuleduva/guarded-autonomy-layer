from sqlalchemy import create_engine
from sqlmodel import SQLModel, Session, select

from src.models.monitoring import RequestAttemptEvent
from src.services import monitoring
from src.services.parser_workflow import run_parser_workflow


def test_retry_conversation_survives_database_persistence():
    messages = [{"role": "system", "content": "Parse."},
                {"role": "user", "content": "Move /demo/a to /demo/b"}]
    calls = []

    class Parser:
        def invoke(self, sent):
            calls.append(list(sent))
            return {"parameters": {} if len(calls) == 1 else {"destination": "/demo/b"}}

    def validate(value):
        if not value["parameters"]:
            raise ValueError("move destination was not specified in the request")

    attempt, token = monitoring.begin()
    try:
        run_parser_workflow(
            parser=Parser(), messages=messages,
            parse_value=lambda value: (value, None), validate_action=validate,
            classify_validation_error=lambda exc: "retry",
            on_attempt=lambda n, status, error, duration, errors, trace: monitoring.record(
                "parser_agent_attempt", status, validation_errors=errors, parser_trace=trace),
        )
    finally:
        monitoring.end(token)
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine, tables=[RequestAttemptEvent.__table__])
    monitoring.persist(attempt, engine)
    with Session(engine) as session:
        rows = session.exec(select(RequestAttemptEvent).order_by(RequestAttemptEvent.sequence)).all()
        assert len(rows) == 2
        first, second = [row.parser_trace for row in rows]
        assert first["original_prompt"] == messages[1]["content"]
        assert first["response"] == {"parameters": {}}
        assert first["correction_scheduled"] is True
        assert second["messages"] == calls[1]
        assert second["messages"][-1]["content"] == first["correction_message"]
        assert second["response"]["parameters"]["destination"] == "/demo/b"
        assert second["correction_message"] is None
