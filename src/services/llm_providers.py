"""LangChain-backed structured chat providers; embeddings have a separate adapter."""
from pydantic import ValidationError

from src.config import settings
from src.services.llm_routing import current_routing, setting_value
from src.services.llm_errors import LLMConfigurationError, StructuredOutputError


class LangChainStructuredParser:
    """Small adapter preserving the existing ``invoke(messages)`` interface."""

    def __init__(self, schema, *, provider, adapter, api_key, base_url, model, timeout=20,
                 max_tokens=1024, max_retries=0):
        self.schema = schema
        self.provider = provider
        self.adapter = adapter
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.max_retries = max_retries

    def _chat_model(self):
        if self.adapter == "sarvam":
            from langchain_sarvam import ChatSarvam

            # Sarvam defaults reasoning on. Its tokens share the output budget;
            # explicitly send null so structured responses have room to finish.
            # Cap at the starter-plan ceiling unless the user raises the setting.
            return ChatSarvam(
                model=self.model,
                api_key=self.api_key,
                temperature=0,
                max_tokens=min(self.max_tokens, settings.SARVAM_LLM_MAX_TOKENS),
                max_retries=self.max_retries,
                model_kwargs={"reasoning_effort": None},
            )
        if self.adapter == "google_genai":
            from langchain_google_genai import ChatGoogleGenerativeAI

            return ChatGoogleGenerativeAI(
                model=self.model,
                google_api_key=self.api_key,
                temperature=0,
                max_output_tokens=self.max_tokens,
                timeout=self.timeout,
                max_retries=self.max_retries,
            )
        if self.adapter == "groq":
            from langchain_groq import ChatGroq

            return ChatGroq(
                model=self.model,
                api_key=self.api_key,
                temperature=0,
                max_tokens=self.max_tokens,
                request_timeout=self.timeout,
                max_retries=self.max_retries,
            )
        if self.adapter == "openai_compatible":
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(
                model=self.model,
                api_key=self.api_key,
                base_url=self.base_url,
                temperature=0,
                max_tokens=self.max_tokens,
                timeout=self.timeout,
                max_retries=self.max_retries,
            )
        raise ValueError(f"unsupported chat provider: {self.provider}")

    def invoke(self, messages):
        from src.db.transaction_guard import assert_no_transaction
        assert_no_transaction()
        try:
            model = self._chat_model()
            structured = model.with_structured_output(self.schema, include_raw=True)
        except (ValueError, TypeError) as exc:
            raise LLMConfigurationError("LLM adapter configuration failed validation") from exc

        try:
            envelope = structured.invoke([
                (message["role"], message["content"]) for message in messages
            ])
        except ValidationError as exc:
            # With include_raw=True, response parsing errors are returned in the
            # envelope. A validation error raised here belongs to the SDK call.
            raise LLMConfigurationError("LLM SDK invocation failed validation") from exc
        if not isinstance(envelope, dict) or not {"raw", "parsed", "parsing_error"}.issubset(envelope):
            raise LLMConfigurationError("LLM adapter returned an invalid structured-output envelope")
        raw = envelope["raw"]
        parsing_error = envelope["parsing_error"]
        if parsing_error is not None:
            if not isinstance(parsing_error, Exception):
                raise LLMConfigurationError("LLM adapter returned an invalid parsing error")
            raise StructuredOutputError(
                f"Model response failed structured-output parsing ({type(parsing_error).__name__}): {parsing_error}",
                raw_output=raw,
            ) from parsing_error
        result = envelope["parsed"]
        if isinstance(result, self.schema):
            return result.model_dump()
        try:
            return self.schema.model_validate(result).model_dump()
        except ValidationError as exc:
            raise StructuredOutputError(
                "Model response failed schema validation",
                raw_output=raw if raw is not None else result,
            ) from exc


def structured_parser(schema, *, provider, model, timeout=20,
                      max_tokens=1024, max_retries=0):
    if not isinstance(model, str) or not model.strip():
        raise ValueError('chat model must not be empty')
    model = model.strip()
    if settings.OFFLINE_MODE:
        raise ValueError("chat providers are unavailable in offline mode")
    providers = current_routing().providers
    if provider not in providers:
        raise ValueError(f"unsupported chat provider: {provider}")
    configured = providers[provider]
    api_key = setting_value(configured.api_key_setting)
    if not api_key:
        raise ValueError(f"configured {provider} chat provider key is missing")
    base_url = setting_value(configured.base_url_setting) if configured.base_url_setting else None
    if configured.adapter == 'openai_compatible' and not base_url:
        raise ValueError(f"configured {provider} chat provider URL is missing")
    return LangChainStructuredParser(
        schema,
        provider=provider,
        adapter=configured.adapter,
        api_key=api_key,
        base_url=base_url,
        model=model,
        timeout=timeout,
        max_tokens=max_tokens,
        max_retries=max_retries,
    )
