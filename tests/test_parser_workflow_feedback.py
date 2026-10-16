import json

import pytest

from src.services.llm_parser import ParsedAction, _validate_grounded_action
from src.services.parser_workflow import _feedback, run_parser_workflow


def payload(message):
    return json.loads(message.split("\n\n", 2)[2])


def test_feedback_preserves_complete_response_and_pydantic_errors():
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as caught:
        ParsedAction.model_validate({"action_class": "move_file"})
    previous = {"content": "x" * 5000}
    data = payload(_feedback(caught.value, previous))
    assert data["previous_response"] == previous
    assert data["validation_errors"]["target_resource"][0]["code"] == "missing"


def test_retry_accumulates_feedback_and_corrects_grounding():
    original = [
        {"role": "system", "content": "Parse the action."},
        {"role": "user", "content": "Move /demo/a.txt to /demo/b.txt"},
    ]
    responses = [
        {"action_class": "move_file", "target_resource": "/demo/a.txt", "parameters": params}
        for params in ({}, {"destination": "/demo/invented.txt"}, {"destination": "/demo/b.txt"})
    ]
    calls = []

    class Parser:
        def invoke(self, messages):
            calls.append(list(messages))
            return responses[len(calls) - 1]

    result = run_parser_workflow(
        parser=Parser(), messages=original,
        parse_value=lambda value: (ParsedAction.model_validate(value), None),
        validate_action=lambda action: _validate_grounded_action(action, original[1]["content"]),
        classify_validation_error=lambda exc: "retry",
    )
    assert result.attempts == 3
    assert calls[1][:2] == original
    assert calls[2][:-1] == calls[1]
    for index, code in enumerate(("missing_destination", "ungrounded_destination"), 1):
        feedback = payload(calls[index][-1]["content"])
        assert feedback["validation_errors"]["parameters.destination"][0]["code"] == code
        assert feedback["previous_response"] == responses[index - 1]
