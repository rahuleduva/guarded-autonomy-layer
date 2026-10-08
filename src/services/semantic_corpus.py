"""Validated, versioned examples shared by cloud seeding and retrieval."""
from functools import lru_cache
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from src.config import PROJECT_ROOT
from src.models.request import ActionRequest

ACTION_FAMILIES = {
    "read_file": "read", "create_file": "create", "create_directory": "create",
    "update_file": "update", "move_file": "move",
    "delete_file": "delete", "delete_directory": "delete",
}
RISK_REASONS = {
    "mass_deletion": "possible mass deletion",
    "review_bypass": "possible attempt to bypass review",
    "secret_export": "possible disclosure of credentials",
    "credential_access": "possible access to credential material",
}
CORPUS_PATH = PROJECT_ROOT / "examples/semantic_advisory_examples.json"


def action_family(action_class: str) -> str:
    try:
        return ACTION_FAMILIES[action_class]
    except KeyError:
        raise ValueError("unsupported action for semantic classification") from None


def example_request(data: dict) -> ActionRequest:
    return ActionRequest.model_validate({
        "agent_id": "semantic-example", "actor_role": "workspace_agent", **data,
    })


class AdvisoryExample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    action_family: Literal["read", "create", "update", "move", "delete", "any"]
    label: Literal["benign", "risky"]
    risk_category: Literal["none", "mass_deletion", "review_bypass", "secret_export", "credential_access"]
    request: dict

    @model_validator(mode="after")
    def validate_metadata(self):
        family = action_family(example_request(self.request).action_class)
        if self.action_family not in {family, "any"}:
            raise ValueError("example action family does not match its operation")
        if (self.label == "benign") != (self.risk_category == "none"):
            raise ValueError("example label and risk category disagree")
        if self.risk_category == "mass_deletion" and self.action_family != "delete":
            raise ValueError("mass deletion examples must be scoped to deletion")
        return self


class AdvisoryCorpus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str
    origin: str
    reviewed: bool
    limitation: str
    examples: list[AdvisoryExample]

    @model_validator(mode="after")
    def validate_examples(self):
        if not self.examples or len({row.id for row in self.examples}) != len(self.examples):
            raise ValueError("corpus requires nonempty, unique example IDs")
        if {row.label for row in self.examples} != {"risky", "benign"}:
            raise ValueError("corpus requires both risky and benign examples")
        return self

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()


@lru_cache(maxsize=1)
def load_corpus() -> AdvisoryCorpus:
    return AdvisoryCorpus.model_validate_json(CORPUS_PATH.read_text())
