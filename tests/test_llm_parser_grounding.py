"""The online parser must not turn unsupported text into executable arguments."""
from types import SimpleNamespace

import pytest

from src.config import settings
from src.models.request import NaturalLanguageRequest
from src.services import llm_parser


def test_exhausted_structured_output_errors_become_recordable_parser_rejections(monkeypatch):
    from src.services import llm_routing, monitoring
    from src.services.llm_errors import StructuredOutputError

    monkeypatch.setattr(settings, 'OFFLINE_MODE', False)
    monkeypatch.setattr(settings, 'SEMANTIC_PIPELINE_MODE', 'baseline')
    calls = []
    raw = {'action_class': 'read_file'}
    def invoke(messages):
        calls.append(list(messages))
        raise StructuredOutputError('Model omitted target_resource', raw_output=raw)
    profile = SimpleNamespace(name='test', provider='test', model='fake')
    parser = SimpleNamespace(invoke=invoke, last_profile=profile, profiles=[profile], failures=[],
        purpose=SimpleNamespace(max_output_attempts=3, max_provider_retries_per_attempt=1,
                                stage_timeout_seconds=120), can_retry=lambda _: False)
    monkeypatch.setattr(llm_routing, 'RoutedStructuredParser', lambda *args: parser)
    attempt, token = monitoring.begin()
    try:
        with pytest.raises(llm_parser.ParserRejectionError) as caught:
            llm_parser.parse(NaturalLanguageRequest(agent_id='a', actor_role='workspace_agent',
                raw_prompt='Read /workspace/notes.txt'))
    finally:
        monitoring.end(token)
    assert len(calls) == 3
    assert caught.value.parser_attempts == 3
    assert caught.value.parser_output_attempts == 3
    assert caught.value.parser_feedback == attempt.events[-1]['parser_trace']['correction_message']
    assert caught.value.validation_errors
    assert all(event['parser_trace']['response'] == raw for event in attempt.events)


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


@pytest.mark.parametrize("ending", ["", ".", ". Then stop.", ", please.", "; then stop."])
def test_delete_path_with_sentence_punctuation_is_grounded(ending):
    target = "/srv/agent/workspace/tmp/cache-note.txt"
    llm_parser._validate_grounded_action(
        action(action_class="delete_file", target_resource=target),
        "Delete the file " + target + ending,
    )


@pytest.mark.parametrize("mentioned", [
    "/srv/agent/workspace/tmp/cache-note.txt.bak",
    "/srv/agent/workspace/tmp/cache-note.txt/child",
    "/srv/agent/workspace/tmp/cache-note.txt-backup",
    "/srv/agent/workspace/tmp/cache-note.txt_backup",
    "/srv/agent/workspace/tmp/cache-note.txt~",
    "/other/srv/agent/workspace/tmp/cache-note.txt",
])
def test_delete_path_cannot_match_part_of_a_longer_path(mentioned):
    with pytest.raises(llm_parser.UnverifiedActionError, match="target path"):
        llm_parser._validate_grounded_action(
            action(action_class="delete_file",
                   target_resource="/srv/agent/workspace/tmp/cache-note.txt"),
            "Delete the file " + mentioned + ".",
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
    from src.services import llm_routing
    monkeypatch.setattr(settings, "OFFLINE_MODE", False)
    monkeypatch.setattr(settings, "SEMANTIC_PIPELINE_MODE", "baseline")
    monkeypatch.setattr(llm_routing, "make_parser", lambda *args: SimpleNamespace(
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
