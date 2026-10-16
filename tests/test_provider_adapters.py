import sys
from types import ModuleType, SimpleNamespace
import pytest
from src.config import settings
from src.services import embeddings, llm_providers
from src.services.llm_parser import ParsedAction


@pytest.mark.parametrize('adapter,module_name,class_name,expected_key', [
    ('openai_compatible', 'langchain_openai', 'ChatOpenAI', 'api_key'),
    ('google_genai', 'langchain_google_genai', 'ChatGoogleGenerativeAI', 'google_api_key'),
    ('groq', 'langchain_groq', 'ChatGroq', 'api_key'),
    ('sarvam', 'langchain_sarvam', 'ChatSarvam', 'api_key'),
])
def test_langchain_adapter_receives_provider_credentials_and_model(monkeypatch,
        adapter, module_name, class_name, expected_key):
    captured = {}
    class FakeChat:
        def __init__(self, **kwargs):
            captured.update(kwargs)
        def with_structured_output(self, schema, *, include_raw):
            captured['schema'] = schema
            captured['include_raw'] = include_raw
            return self
        def invoke(self, messages):
            captured['messages'] = messages
            value = {'action_class':'read_file', 'target_resource':'notes.txt'}
            return {'raw': value, 'parsed': value, 'parsing_error': None}
    module = ModuleType(module_name)
    setattr(module, class_name, FakeChat)
    monkeypatch.setitem(sys.modules, module_name, module)
    parser = llm_providers.LangChainStructuredParser(ParsedAction,
        provider='test', adapter=adapter, api_key='synthetic-key',
        base_url='https://example.invalid/v1', model='test-model')
    result = parser.invoke([{'role':'user','content':'read notes.txt'}])
    assert result['action_class'] == 'read_file'
    assert captured['model'] == 'test-model'
    assert str(captured[expected_key]) == 'synthetic-key'
    assert captured['schema'] is ParsedAction
    assert captured['include_raw'] is True
    assert captured['messages'] == [('user', 'read notes.txt')]
    if adapter == 'openai_compatible':
        assert captured['base_url'] == 'https://example.invalid/v1'


def test_langchain_parsing_failure_retains_full_raw_response(monkeypatch):
    from src.services.llm_errors import StructuredOutputError
    from src.services.parser_workflow import _raw_output, _validation_errors
    from pydantic import ValidationError

    raw = {'content': '', 'tool_calls': [{'name': 'ParsedAction',
                                         'args': {'action_class': 'move_file'}}]}
    with pytest.raises(ValidationError) as invalid:
        ParsedAction.model_validate(raw['tool_calls'][0]['args'])
    envelope = {'raw': raw, 'parsed': None, 'parsing_error': invalid.value}
    model = SimpleNamespace(with_structured_output=lambda *args, **kwargs:
                            SimpleNamespace(invoke=lambda _: envelope))
    parser = llm_providers.LangChainStructuredParser(ParsedAction,
        provider='test', adapter='test', api_key='synthetic', base_url=None, model='test')
    monkeypatch.setattr(parser, '_chat_model', lambda: model)
    with pytest.raises(StructuredOutputError) as caught:
        parser.invoke([{'role': 'user', 'content': 'Move the file'}])
    assert caught.value.__cause__ is invalid.value
    assert _raw_output(caught.value) == raw
    assert _validation_errors(caught.value)['target_resource'][0]['code'] == 'missing'


def test_sdk_validation_failure_stops_without_output_corrections(monkeypatch):
    from pydantic import ValidationError
    from src.services.llm_errors import LLMConfigurationError
    from src.services.parser_workflow import run_parser_workflow
    from src.services.semantic_failures import handled, is_validation_failure

    calls, events = [], []
    def invalid_configuration():
        calls.append('construct')
        ParsedAction.model_validate({})
    parser = llm_providers.LangChainStructuredParser(ParsedAction,
        provider='test', adapter='test', api_key='synthetic', base_url=None, model='test')
    monkeypatch.setattr(parser, '_chat_model', invalid_configuration)
    with pytest.raises(LLMConfigurationError) as caught:
        run_parser_workflow(parser=parser, messages=[{'role': 'user', 'content': 'Read notes.txt'}],
            parse_value=lambda value: (value, None), validate_action=lambda _: None,
            classify_validation_error=lambda _: 'retry',
            on_attempt=lambda *args: events.append(args[-1]),
            max_output_attempts=3, max_provider_retries_per_attempt=2)
    assert isinstance(caught.value.__cause__, ValidationError)
    assert calls == ['construct']
    assert not handled(caught.value)
    assert not is_validation_failure(caught.value)
    assert events[0]['error_origin'] == 'internal'
    assert events[0]['failure_category'] == 'application_error'
    assert events[0]['correction_message'] is None


def test_gemini_embedding_normalizes_and_closes_client(monkeypatch):
    captured = {}
    class FakeClient:
        def __init__(self, **kwargs):
            self.models = SimpleNamespace(embed_content=self.embed)
        def embed(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(embeddings=[SimpleNamespace(values=[3.0, 4.0])])
        def close(self):
            captured["closed"] = True
    google = ModuleType("google")
    genai = ModuleType("google.genai")
    genai.Client = FakeClient
    genai.types = SimpleNamespace(HttpOptions=lambda **kwargs: kwargs, EmbedContentConfig=lambda **kwargs: kwargs)
    google.genai = genai
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.genai", genai)
    monkeypatch.setattr(settings, "OFFLINE_MODE", False)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "synthetic-test-key")
    monkeypatch.setattr(settings, "GEMINI_EMBEDDING_DIMENSIONS", 2)
    assert embeddings.gemini_embed("test") == [0.6, 0.8]
    assert captured["closed"]
    assert captured["contents"] == "task: sentence similarity | query: test"
    assert captured["config"]["output_dimensionality"] == 2
