"""Provider-backed structured action parsing, never policy decisions."""
import json
from src.config import settings


class GeminiStructuredParser:
    def __init__(self, schema, *, api_key, model):
        self.schema = schema
        self.api_key, self.model = api_key, model

    def invoke(self, messages):
        from google import genai
        from google.genai import types

        system_instruction = "\n".join(
            message["content"] for message in messages if message["role"] == "system"
        )
        user_content = "\n\n".join(
            message["content"] for message in messages if message["role"] == "user"
        )
        client = genai.Client(api_key=self.api_key,
                              http_options=types.HttpOptions(timeout=10000))
        try:
            response = client.models.generate_content(
                model=self.model,
                contents=user_content,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json",
                    response_json_schema=self.schema.model_json_schema(),
                    temperature=0,
                    max_output_tokens=1024,
                ),
            )
        finally:
            client.close()
        if isinstance(response.parsed, self.schema):
            return response.parsed.model_dump()
        if response.parsed is not None:
            return self.schema.model_validate(response.parsed).model_dump()
        if not response.text:
            raise ValueError("provider did not return an action object")
        return self.schema.model_validate_json(response.text).model_dump()


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
        # JSON mode guarantees syntactically valid JSON, not the requested
        # field names. The caller performs schema validation after applying
        # any provider-shape compatibility mapping.
        return json.loads(message.content)


def structured_parser(schema):
    if settings.OFFLINE_MODE:
        raise ValueError("chat providers are unavailable in offline mode")
    if settings.LLM_PROVIDER == "groq":
        if not settings.GROQ_API_KEY:
            raise ValueError("configured chat provider key is missing")
        return StructuredParser(schema, api_key=settings.GROQ_API_KEY,
                                base_url="https://api.groq.com/openai/v1",
                                model=settings.GROQ_LLM_MODEL)
    if not settings.GEMINI_API_KEY:
        raise ValueError("configured chat provider key is missing")
    return GeminiStructuredParser(schema, api_key=settings.GEMINI_API_KEY,
                                  model=settings.GEMINI_LLM_MODEL)
