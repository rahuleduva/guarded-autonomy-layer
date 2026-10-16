"""Error boundaries between adapter configuration and rejected model output."""
from copy import deepcopy


class LLMConfigurationError(RuntimeError):
    """The adapter or SDK failed validation before returning a parsed response."""


class StructuredOutputError(ValueError):
    """A received model response failed structured-output parsing or validation."""

    failure_category = "structured_output_invalid"

    def __init__(self, message, *, raw_output):
        super().__init__(message)
        if hasattr(raw_output, "model_dump"):
            raw_output = raw_output.model_dump(mode="json")
        self.raw_output = deepcopy(raw_output)
