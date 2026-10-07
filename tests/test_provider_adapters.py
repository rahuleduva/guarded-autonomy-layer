import sys
from types import ModuleType, SimpleNamespace
import pytest
from src.config import settings
from src.services import embeddings, llm_providers
from src.services.llm_parser import ParsedAction


@pytest.mark.parametrize("provider,base_url,key_field,model_field", [
    ("groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY", "GROQ_LLM_MODEL"),
    ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/", "GEMINI_API_KEY", "GEMINI_LLM_MODEL"),
])
def test_configured_chat_provider_uses_validated_json(monkeypatch, provider, base_url, key_field, model_field):
    captured = {}
    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        def __enter__(self):
            return self
        def __exit__(self, *args):
            captured["closed"] = True
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                content='{"action_class":"read_file","target_resource":"notes.txt"}'))])
    module = ModuleType("openai")
    module.OpenAI = FakeClient
    monkeypatch.setitem(sys.modules, "openai", module)
    monkeypatch.setattr(settings, "OFFLINE_MODE", False)
    monkeypatch.setattr(settings, "LLM_PROVIDER", provider)
    monkeypatch.setattr(settings, key_field, "synthetic-test-key")
    result = llm_providers.structured_parser(ParsedAction).invoke([{"role": "user", "content": "read notes.txt"}])
    assert result.action_class == "read_file" and result.target_resource == "notes.txt"
    assert captured["model"] == getattr(settings, model_field)
    assert captured["temperature"] == 0 and captured["timeout"] == 10
    assert captured["base_url"] == base_url and captured["closed"]
    assert captured["response_format"] == {"type": "json_object"}
    assert "action_class" in captured["messages"][0]["content"]


@pytest.mark.parametrize("content,finish_reason", [
    ('{"action_class":"read_file","target_resource":"x","agent_id":"attacker"}', "stop"),
    ('{"action_class":"run_shell","target_resource":"x"}', "stop"),
    ('not json', "stop"),
    ('{"action_class":"read_file","target_resource":"x"}', "length"),
    (None, "stop"),
])
def test_provider_rejects_invalid_or_incomplete_actions(monkeypatch, content, finish_reason):
    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs:
                SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish_reason,
                    message=SimpleNamespace(content=content))])))
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    module = ModuleType("openai")
    module.OpenAI = FakeClient
    monkeypatch.setitem(sys.modules, "openai", module)
    parser = llm_providers.StructuredParser(ParsedAction, api_key="synthetic", base_url="https://example.test", model="test")
    with pytest.raises(ValueError):
        parser.invoke([{"role": "user", "content": "read file x"}])


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
