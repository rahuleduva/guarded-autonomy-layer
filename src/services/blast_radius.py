"""Pure scope estimates, computed from request structure without filesystem I/O."""

import glob
import posixpath
from typing import Any

from src.models.request import ActionRequest


def estimate(request: ActionRequest) -> dict[str, Any]:
    """Describe possible impact; score is a scope index, not a risk category.

    File=1, directory=3, unknown type=5; recursive scope adds 5, a wildcard
    adds 4, and shallow targets add max(0, 3-depth). These weights are frozen
    with the result so replay never needs to rerun this estimator. Counts for
    broad targets remain unknown: caller-supplied file counts are not trusted.
    """
    recursive = request.parameters.get("recursive", False)
    if not isinstance(recursive, bool):
        raise ValueError("parameters.recursive must be a boolean")

    target = posixpath.normpath(request.target_resource)
    depth = len([part for part in target.split("/") if part not in ("", ".", "..")])
    if request.action_class.endswith("_directory") or request.target_resource.endswith("/"):
        target_type = "directory"
    elif request.action_class.endswith("_file"):
        target_type = "file"
    else:
        target_type = "unknown"

    wildcard = glob.has_magic(request.target_resource)
    factors = {
        "target_type": {"file": 1, "directory": 3, "unknown": 5}[target_type],
        "recursive": 5 if recursive else 0,
        "wildcard": 4 if wildcard else 0,
        "shallow_target": max(0, 3 - depth),
    }
    broad = recursive or wildcard or target_type == "unknown"
    warnings = []
    if recursive:
        warnings.append("recursive scope")
    if wildcard:
        warnings.append("wildcard scope")
    if target_type == "unknown":
        warnings.append("unknown target type")

    return {
        "source": "deterministic-scope-v1",
        "target_type": target_type,
        "recursive": recursive,
        "wildcard": wildcard,
        "path_depth": depth,
        "score": sum(factors.values()),
        "score_factors": factors,
        "estimated_resources": None if broad else 1,
        "count_source": "request_structure_only",
        "requires_review": broad,
        "warning": "; ".join(warnings) if warnings else None,
    }
