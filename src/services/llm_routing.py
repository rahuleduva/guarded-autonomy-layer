"""Validated purpose routing for structured chat calls.

Routing contains no secrets. A purpose chooses named models; each model chooses
an adapter-backed provider. A parser graph makes one physical call per turn,
while standalone stages may walk the configured fallback list directly.
"""
from __future__ import annotations

from functools import lru_cache
from time import monotonic
import os
from dotenv import dotenv_values
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
import yaml

from src.config import ENV_FILE, settings
from src.services.llm_errors import LLMConfigurationError


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ProviderConfig(StrictModel):
    adapter: Literal['openai_compatible', 'google_genai', 'groq', 'sarvam']
    api_key_setting: str = Field(pattern=r'^[A-Z][A-Z0-9_]*$')
    base_url_setting: str | None = Field(default=None, pattern=r'^[A-Z][A-Z0-9_]*$')


class ModelProfile(StrictModel):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)


class EmbeddingProfile(StrictModel):
    provider: Literal['sentence_transformers', 'gemini', 'jina', 'aicredits']
    model: str = Field(min_length=1)


class EmbeddingCatalog(StrictModel):
    selected: str = Field(min_length=1)
    models: dict[str, EmbeddingProfile]


class FallbackPolicy(StrictModel):
    http_statuses: list[int] = Field(default_factory=list)
    on_timeout: bool = False
    on_connection_error: bool = False


class PurposeConfig(StrictModel):
    models: list[str] = Field(min_length=1)
    fallback_policy: str
    timeout_seconds: float = Field(gt=0, le=120)
    max_output_tokens: int = Field(ge=128, le=128000)
    max_output_attempts: int = Field(ge=1, le=10)
    max_provider_retries_per_attempt: int = Field(ge=0, le=5)
    stage_timeout_seconds: float = Field(gt=0, le=1200)

    @property
    def max_total_calls(self) -> int:
        return self.max_output_attempts * (1 + self.max_provider_retries_per_attempt)


class RoutingConfig(StrictModel):
    providers: dict[str, ProviderConfig]
    models: dict[str, ModelProfile]
    embeddings: EmbeddingCatalog
    fallback_policies: dict[str, FallbackPolicy]
    purposes: dict[str, PurposeConfig]

    @model_validator(mode='after')
    def validate_references(self):
        if self.embeddings.selected not in self.embeddings.models:
            raise ValueError('selected embedding profile is missing')
        for name, provider in self.providers.items():
            if provider.adapter == 'openai_compatible' and not provider.base_url_setting:
                raise ValueError(f'provider {name} requires a base URL setting')
            if provider.adapter != 'openai_compatible' and provider.base_url_setting:
                raise ValueError(f'provider {name} does not use a base URL setting')
        for name, profile in self.models.items():
            if profile.provider not in self.providers:
                raise ValueError(f'model {name} references unknown provider {profile.provider}')
        for name, purpose in self.purposes.items():
            if purpose.fallback_policy not in self.fallback_policies:
                raise ValueError(f'purpose {name} references unknown fallback policy')
            if len(set(purpose.models)) != len(purpose.models):
                raise ValueError(f'purpose {name} repeats a model profile')
            for profile in purpose.models:
                if profile not in self.models:
                    raise ValueError(f'purpose {name} references unknown model {profile}')
            choices = [(self.models[profile].provider, self.models[profile].model)
                       for profile in purpose.models]
            if len(set(choices)) != len(choices):
                raise ValueError(f'purpose {name} repeats a provider/model choice')
        for required in ('request_parser', 'request_analyzer', 'semantic_judge'):
            if required not in self.purposes:
                raise ValueError(f'missing required LLM purpose: {required}')
        return self


@lru_cache(maxsize=4)
def load_routing(path: str) -> RoutingConfig:
    data = yaml.safe_load(Path(path).read_text(encoding='utf-8'))
    return RoutingConfig.model_validate(data)


def current_routing() -> RoutingConfig:
    return load_routing(str(settings.LLM_ROUTING_FILE))


def setting_value(name: str) -> str:
    """Use Settings for known keys; support additional gateways via env/.env."""
    if hasattr(settings, name):
        return str(getattr(settings, name) or '')
    return os.environ.get(name) or str(dotenv_values(ENV_FILE).get(name) or '')


def validate_runtime_config() -> None:
    """Fail startup for selected online providers lacking required connection settings."""
    routing = current_routing()
    embedding = routing.embeddings.models[routing.embeddings.selected]
    if settings.OFFLINE_MODE:
        return
    embedding_key = {'gemini': 'GEMINI_API_KEY', 'jina': 'JINA_API_KEY',
                     'aicredits': 'AI_CREDITS_API_KEY'}.get(embedding.provider)
    if embedding_key and not setting_value(embedding_key):
        raise ValueError(f'embedding provider {embedding.provider} is missing {embedding_key}')
    selected = {routing.models[model].provider
                for purpose in routing.purposes.values() for model in purpose.models}
    for name in selected:
        provider = routing.providers[name]
        if not setting_value(provider.api_key_setting):
            raise ValueError(f'LLM provider {name} is missing {provider.api_key_setting}')
        if provider.base_url_setting and not setting_value(provider.base_url_setting):
            raise ValueError(f'LLM provider {name} is missing {provider.base_url_setting}')


class ResolvedProfile(StrictModel):
    name: str
    provider: str
    adapter: str
    model: str
    api_key_setting: str
    base_url_setting: str | None = None


def resolve_purpose(name: str, *, routing: RoutingConfig | None = None):
    routing = routing or current_routing()
    if name not in routing.purposes:
        raise ValueError(f'unknown LLM purpose: {name}')
    purpose = routing.purposes[name]
    profiles = []
    for profile_name in purpose.models:
        model = routing.models[profile_name]
        provider = routing.providers[model.provider]
        profiles.append(ResolvedProfile(name=profile_name, provider=model.provider,
            adapter=provider.adapter, model=model.model,
            api_key_setting=provider.api_key_setting,
            base_url_setting=provider.base_url_setting))
    return purpose, profiles, routing.fallback_policies[purpose.fallback_policy]


def http_status(exc: Exception) -> int | None:
    for current in exception_chain(exc):
        for attr in ('status_code', 'code'):
            value = getattr(current, attr, None)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    return None


def exception_chain(exc: Exception):
    current = exc
    seen = set()
    for _ in range(6):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


REFUSAL_NAMES = {'ContentFilterFinishReasonError', 'BlockedPromptException', 'RefusalError',
                 'OpenAIRefusalError'}
TIMEOUT_NAMES = {'APITimeoutError', 'ConnectTimeout', 'ReadTimeout', 'WriteTimeout',
                 'PoolTimeout', 'TimeoutException', 'TimeoutError'}
CONNECTION_NAMES = {'APIConnectionError', 'ConnectError', 'ConnectionError'}


class StageDeadlineExceeded(TimeoutError):
    """The stage's elapsed-time budget has expired; further calls are forbidden."""


def provider_failure_category(exc: Exception) -> str | None:
    """Use one precedence rule for routing, correction and durable failures."""
    chain = list(exception_chain(exc))
    if any(isinstance(item, LLMConfigurationError) for item in chain):
        return None
    if any(type(item).__name__ in REFUSAL_NAMES for item in chain):
        return 'provider_refusal'
    if any(isinstance(item, StageDeadlineExceeded) for item in chain):
        return 'stage_deadline_exceeded'
    status = http_status(exc)
    if status in {408, 409, 425, 429} or status is not None and status >= 500:
        return 'provider_unavailable'
    if any(isinstance(item, (TimeoutError, ConnectionError))
           or type(item).__name__ in TIMEOUT_NAMES | CONNECTION_NAMES for item in chain):
        return 'provider_unavailable'
    if status is not None:
        return 'provider_rejected'
    return None


def can_fallback(exc: Exception, policy: FallbackPolicy) -> bool:
    chain = list(exception_chain(exc))
    if any(isinstance(item, (StageDeadlineExceeded, LLMConfigurationError))
           or type(item).__name__ in REFUSAL_NAMES
           for item in chain):
        return False
    if http_status(exc) in policy.http_statuses:
        return True
    return any((policy.on_timeout and (isinstance(item, TimeoutError) or type(item).__name__ in TIMEOUT_NAMES))
               or (policy.on_connection_error and (isinstance(item, ConnectionError) or type(item).__name__ in CONNECTION_NAMES))
               for item in chain)


def make_parser(schema, profile: ResolvedProfile, purpose: PurposeConfig):
    from src.services.llm_providers import structured_parser
    return structured_parser(schema, provider=profile.provider, model=profile.model,
        timeout=purpose.timeout_seconds, max_tokens=purpose.max_output_tokens,
        max_retries=0)


def invoke_for_purpose(name: str, schema, messages_for_attempt: Callable[[int], list[dict]],
                       *, failures: list[dict] | None = None,
                       on_failure: Callable[[int, ResolvedProfile, Exception], None] | None = None,
                       validate_output: Callable[[BaseModel], None] | None = None):
    """Apply the same two budgets to standalone structured calls."""
    from src.services import monitoring
    from src.services.parser_workflow import run_parser_workflow, _is_structured_output_error
    parser = RoutedStructuredParser(name, schema)
    failures = failures if failures is not None else []

    def on_attempt(attempt, status, exc, duration_ms, validation_errors, trace):
        profile = parser.last_profile
        monitoring.record(name + '_attempt', status, duration_ms=duration_ms,
                          provider=profile.provider, model=profile.model,
                          error_type=type(exc).__name__ if exc else None,
                          http_status=http_status(exc) if exc else None,
                          validation_errors=validation_errors, parser_trace=trace)
        if exc is not None:
            failures.append(dict(provider=profile.provider, model=profile.model,
                                 http_status=http_status(exc), error=type(exc).__name__))
            if on_failure:
                on_failure(attempt - 1, profile, exc)

    result = run_parser_workflow(
        parser=parser, messages=messages_for_attempt(0),
        parse_value=lambda value: (schema.model_validate(value), None),
        validate_action=validate_output or (lambda value: None),
        classify_validation_error=lambda exc: 'retry' if (
            _is_structured_output_error(exc) or validate_output is not None
            and isinstance(exc, ValueError) and provider_failure_category(exc) is None
        ) else 'internal',
        on_attempt=on_attempt,
        max_output_attempts=parser.purpose.max_output_attempts,
        max_provider_retries_per_attempt=parser.purpose.max_provider_retries_per_attempt,
        stage_timeout_seconds=parser.purpose.stage_timeout_seconds,
        retry_provider_error=parser.can_retry,
    )
    return result.parsed, parser.last_profile


class RoutedStructuredParser:
    """One physical call per graph turn with bounded output and provider budgets."""

    def __init__(self, purpose_name: str, schema):
        self.purpose, self.profiles, self.policy = resolve_purpose(purpose_name)
        self.schema = schema
        self.index = 0
        self.calls = 0
        self.deadline: float | None = None
        self.last_profile = self.profiles[0]
        self.failures: list[dict] = []

    def can_retry(self, exc: Exception) -> bool:
        return can_fallback(exc, self.policy)

    def advance_profile(self) -> bool:
        """Move to the next configured model after repeated invalid output."""
        if self.index + 1 >= len(self.profiles):
            return False
        self.index += 1
        return True

    def invoke(self, messages):
        if self.calls >= self.purpose.max_total_calls:
            raise RuntimeError('LLM call budget exhausted')
        purpose = self.purpose
        if self.deadline is not None:
            remaining = self.deadline - monotonic()
            if remaining <= 0:
                raise StageDeadlineExceeded('LLM stage time budget exhausted')
            purpose = purpose.model_copy(update={'timeout_seconds': min(purpose.timeout_seconds, remaining)})
        profile = self.profiles[self.index]
        self.last_profile = profile
        self.calls += 1
        try:
            return make_parser(self.schema, profile, purpose).invoke(messages)
        except Exception as exc:
            self.failures.append(dict(provider=profile.provider, model=profile.model,
                                      http_status=http_status(exc), error=type(exc).__name__))
            raise
