"""Versioned encrypted input contract; never supplied by an HTTP caller."""
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from src.models.request import ActionRequest, NaturalLanguageRequest, ClarificationAnswer, ValidatedContract


class WorkflowCaller(ValidatedContract):
    principal_id: str = Field(min_length=1)
    kind: Literal['agent', 'human']
    agent_id: str | None
    actor_role: str | None
    auth_version: int = Field(ge=1, strict=True)
    credential_id: str | None
    expires_at: datetime | None
    correlation_id: str = Field(min_length=1)
    route: str = Field(min_length=1)


class BackgroundInput(ValidatedContract):
    version: Literal['background-input-v1']
    kind: Literal['evaluate', 'interpret', 'clarify', 'rollback', 'prepare_review']
    scope: str = Field(min_length=1)
    key: str = Field(min_length=1, max_length=200)
    payload: dict[str, Any]
    normalized_request: dict[str, Any] | None
    caller: WorkflowCaller

    @model_validator(mode='after')
    def request_contract(self):
        if self.kind in {'evaluate', 'interpret', 'clarify'}:
            model = ActionRequest if self.kind == 'evaluate' else NaturalLanguageRequest
            if self.normalized_request is None or not model.model_fields.keys() <= self.normalized_request.keys():
                raise ValueError('background input requires all normalized request fields, including generated defaults')
            frozen = model.model_validate(self.normalized_request).model_dump(mode='json')
            if self.kind == 'clarify':
                answer = ClarificationAnswer.model_validate(self.payload)
                if (frozen['agent_id'], frozen['actor_role'], frozen['raw_prompt']) != (
                        answer.agent_id, answer.actor_role, answer.clarified_prompt):
                    raise ValueError('clarification source differs from its answer')
            else:
                submitted = model.model_validate({**self.normalized_request, **self.payload}).model_dump(mode='json')
                if any(frozen[key] != submitted[key] for key in self.payload):
                    raise ValueError('normalized request differs from submitted fields')
            if (frozen['agent_id'], frozen['actor_role']) != (self.caller.agent_id, self.caller.actor_role):
                raise ValueError('frozen request differs from its initiating identity')
        else:
            field = 'execution_id' if self.kind == 'rollback' else 'decision_id'
            if self.payload != {field: self.scope} or self.normalized_request is not None:
                raise ValueError('workflow arguments differ from their business scope')
        return self
