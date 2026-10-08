"""The online parser must not turn unsupported text into executable arguments."""
from types import SimpleNamespace

import pytest

from src.config import settings
from src.models.request import NaturalLanguageRequest
from src.services import llm_parser


def action(action_class="create_file", target_resource="/srv/agent/workspace/note.txt",
           parameters=None, environment="production"):
    return llm_parser.ParsedAction(
        action_class=action_class, target_resource=target_resource,
        parameters={} if parameters is None else parameters, environment=environment,
    )


def test_explicit_content_and_composed_path_are_grounded():
    llm_parser._validate_grounded_action(
        action(parameters={"content": "Hello team."}, environment="development"),
        "Create note.txt in /srv/agent/workspace with content 'Hello team.' "
        "Use the development environment.",
    )


@pytest.mark.parametrize("parameters,prompt", [
    ({"content": "Invented release notes"},
     "Create /srv/agent/workspace/note.txt with a summary of the release notes."),
    ({}, "Create /srv/agent/workspace/note.txt with a summary of the release notes."),
    ({"content": "summary"},
     "Create /srv/agent/workspace/note.txt with a short summary of the release notes."),
    ({"content": "hello"},
     "Create /srv/agent/workspace/note.txt with content Hello"),
])
def test_missing_or_unsourced_content_is_rejected(parameters, prompt):
    with pytest.raises(llm_parser.UnverifiedActionError):
        llm_parser._validate_grounded_action(action(parameters=parameters), prompt)


def test_empty_file_must_be_explicitly_requested():
    prompt = "Create an empty file /srv/agent/workspace/note.txt"
    llm_parser._validate_grounded_action(action(parameters={"content": ""}), prompt)
    with pytest.raises(llm_parser.UnverifiedActionError):
        llm_parser._validate_grounded_action(
            action(parameters={"content": ""}),
            "Create file /srv/agent/workspace/note.txt",
        )


@pytest.mark.parametrize("proposed,prompt", [
    (action(target_resource="/srv/agent/workspace/note.txt", parameters={"content": "Hi"}),
     "Create /srv/agent/workspace/other.txt with content Hi"),
    (action(action_class="move_file", parameters={"destination": "/srv/agent/workspace/archive/note.txt"}),
     "Move /srv/agent/workspace/note.txt to /srv/agent/workspace/other.txt"),
    (action(action_class="move_file", parameters={}),
     "Move /srv/agent/workspace/note.txt"),
    (action(action_class="read_file", parameters={"content": "extra"}),
     "Read /srv/agent/workspace/note.txt"),
    (action(action_class="delete_directory", parameters={"recursive": True}),
     "Delete /srv/agent/workspace/note.txt"),
    (action(action_class="read_file", environment="development"),
     'Read /srv/agent/workspace/note.txt and quote "use development".'),
])
def test_unsupported_or_invented_arguments_are_rejected(proposed, prompt):
    with pytest.raises(llm_parser.UnverifiedActionError):
        llm_parser._validate_grounded_action(proposed, prompt)


def test_online_parser_rejects_unsourced_content_before_evaluation(monkeypatch):
    monkeypatch.setattr(settings, "OFFLINE_MODE", False)
    monkeypatch.setattr(settings, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(llm_parser, "structured_parser", lambda _: SimpleNamespace(
        invoke=lambda _: action(parameters={"content": "Fabricated summary."}).model_dump(),
    ))
    request = NaturalLanguageRequest(
        agent_id="agent", actor_role="workspace_agent",
        raw_prompt="Create /srv/agent/workspace/note.txt with a summary of release notes.",
    )
    with pytest.raises(llm_parser.UnverifiedActionError):
        llm_parser.parse(request)


def test_conflicting_provider_content_is_rejected():
    with pytest.raises(ValueError, match="conflicting file content"):
        llm_parser._adapt_provider_action({
            "action_class": "create_file", "target_resource": "note.txt",
            "parameters": {"content": "one"}, "content": "two",
        })
