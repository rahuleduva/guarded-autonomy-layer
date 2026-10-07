"""Pure action binding for recorded human approval."""
import hashlib
import json
from src.models.request import ActionRequest


def fingerprint(request: ActionRequest) -> str:
    content = request.model_dump(mode="json", exclude={"request_id", "timestamp"})
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
