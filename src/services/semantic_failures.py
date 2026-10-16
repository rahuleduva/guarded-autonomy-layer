"""Structured semantic dependency failures, without credential-bearing transport text."""
from pydantic import ValidationError
from src.models.semantic import SemanticFailure
from src.services.llm_errors import LLMConfigurationError, StructuredOutputError
from src.services.llm_routing import exception_chain, http_status, provider_failure_category


class SemanticValidationError(ValueError):
    """A locally authored semantic validation error whose message contains no transport secrets."""

    def __init__(self, message, *, failure_category='semantic_validation_invalid',
                 validation_errors=None):
        super().__init__(message)
        self.failure_category = failure_category
        self.validation_errors = validation_errors


def is_validation_failure(exc):
    if any(isinstance(e, LLMConfigurationError) for e in exception_chain(exc)):
        return False
    if provider_failure_category(exc) is not None:
        return False
    return any(isinstance(e, (ValidationError, SemanticValidationError, StructuredOutputError))
               or type(e).__name__ == 'OutputParserException' for e in exception_chain(exc))


def handled(exc):
    """Only expected dependency and data-validation failures become review outcomes."""
    if any(isinstance(e, LLMConfigurationError) for e in exception_chain(exc)):
        return False
    return any(isinstance(e, (ValueError, TimeoutError, ConnectionError))
               or http_status(e) is not None
               or type(e).__module__.split('.')[0] in {
                   'openai', 'httpx', 'httpcore', 'google', 'groq', 'qdrant_client',
                   'requests', 'langchain_core', 'langchain_google_genai', 'grpc'}
               for e in exception_chain(exc))


def failure(exc, code, message):
    provider_category = provider_failure_category(exc)
    errors = None if provider_category else getattr(exc, 'validation_errors', None)
    # LangChain's Pydantic output parser wraps schema errors in an
    # OutputParserException and chains the original ValidationError as its
    # cause. Classify the outer exception, but keep the useful field-level
    # detail from the underlying Pydantic error when one is available.
    validation_error = next(
        (item for item in exception_chain(exc) if not provider_category and isinstance(item, ValidationError)),
        None,
    )
    if errors is None and validation_error is not None:
        errors = {}
        for item in validation_error.errors(
            include_input=False, include_context=False, include_url=False
        ):
            key = '.'.join(map(str, item['loc'])) or 'non_field_errors'
            errors.setdefault(key, []).append({'code': item['type'], 'message': item['msg']})
    elif errors is None and not provider_category and isinstance(exc, ValueError):
        # SDK ValueErrors can echo connection/header values. Only our locally
        # authored validation messages are safe to copy; preserve structured
        # Pydantic details above without input values or exception context.
        errors = {'non_field_errors': [{'code': code, 'message':
            str(exc) if isinstance(exc, SemanticValidationError) else message}]}
    category = provider_category or getattr(exc, 'failure_category', None) or (
        'structured_output_invalid' if is_validation_failure(exc) else 'application_error')
    return SemanticFailure(code=code, exception_type=type(exc).__name__, message=message,
                           failure_category=category,
                           http_status=http_status(exc), validation_errors=errors).model_dump(mode='json')
