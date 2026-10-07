"""OpenAI-compatible chat providers used for parsing, never policy decisions."""
import json
from src.config import settings


class StructuredParser:
    def __init__(self, schema, *, api_key, base_url, model):
        self.schema = schema
        self.api_key, self.base_url, self.model = api_key, base_url, model

    def invoke(self, messages):
        from openai import OpenAI
        # JSON mode supports the action's open-ended parameters dictionary.
        # Pydantic owns schema validation; JSON mode alone does not enforce it.
        instructions = {
            "role": "system",
            "content": "Return exactly one JSON object conforming to this schema: "
                       + json.dumps(self.schema.model_json_schema()),
        }
        with OpenAI(api_key=self.api_key, base_url=self.base_url,
                    max_retries=2, timeout=10) as client:
            response = client.chat.completions.create(
                model=self.model, messages=[instructions, *messages],
                temperature=0, max_tokens=1024,
                response_format={"type": "json_object"},
            )
        if not response.choices or response.choices[0].finish_reason != "stop":
            raise ValueError("provider did not return a complete action")
        message = response.choices[0].message
        if getattr(message, "refusal", None) or not message.content:
            raise ValueError("provider did not return an action object")
        return self.schema.model_validate_json(message.content)


def structured_parser(schema):
    if settings.OFFLINE_MODE:
        raise ValueError("chat providers are unavailable in offline mode")
    if settings.LLM_PROVIDER == "groq":
        key, base_url, model = settings.GROQ_API_KEY, "https://api.groq.com/openai/v1", settings.GROQ_LLM_MODEL
    else:
        key, base_url, model = settings.GEMINI_API_KEY, "https://generativelanguage.googleapis.com/v1beta/openai/", settings.GEMINI_LLM_MODEL
    if not key:
        raise ValueError("configured chat provider key is missing")
    return StructuredParser(schema, api_key=key, base_url=base_url, model=model)
