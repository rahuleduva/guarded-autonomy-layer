"""Pure algebraic sandbox containment (Phase 1, finding A1).

Location-independent by construction: this module performs **no filesystem
access at all** -- no ``realpath``, no ``stat``, no existence checks, no
directory creation. A verdict depends only on the two input strings, so the
same check guards a POSIX sandbox or an object-storage key space, on any
machine.

``sandbox_root`` is the evaluated policy's declared scope (C.4) -- part of
the artifact and therefore hashed into ``policy_hash``, never read from
config or derived from the app's location. Callers pass that policy root as
``sandbox_root`` and anchor relative ``target_resource`` values with
:func:`to_absolute` first, so the guard only reasons about absolute paths.

Known limitation (Phase 11): paths are treated as strings, so symlink
escapes are not detected. Accepted deliberately -- A1's core is the naive
``startswith`` prefix bug, which ``normpath`` + ``commonpath`` fixes exactly.
"""

import os
from typing import Optional


def to_absolute(base_dir: str, target: str) -> Optional[str]:
    """Anchor ``target`` to the policy's absolute ``sandbox_root``.

    This is the only place a relative path is resolved, and it anchors to
    ``base_dir`` -- never to the current directory. Pass incoming
    ``target_resource`` values through it before calling
    :func:`is_within_sandbox`.

    Returns ``None`` when ``base_dir`` is not absolute, which is a
    misconfiguration the caller must treat as a denial. Returning ``None``
    is what keeps the cwd out of the decision: a relative ``base_dir`` would
    make the joined path relative, and normalising *that* would resolve
    against the working directory.
    """
    if not os.path.isabs(base_dir):
        return None
    if os.path.isabs(target):
        return target
    return os.path.normpath(os.path.join(base_dir, target))


def is_within_sandbox(target: str, sandbox_root: str) -> bool:
    """Return True only when ``target`` is inside ``sandbox_root``.

    ``sandbox_root`` is the evaluated policy's absolute scope. Both
    arguments must be absolute; the ``isabs`` check runs *before* any
    normalisation, so a relative value denies rather than resolving against
    the process working directory.

    ``normpath`` collapses ``..`` segments, so traversal is reduced to
    ordinary path algebra. ``commonpath`` compares components rather than
    string prefixes, which is what rejects the ``<root>-evil`` lookalike
    that a naive ``startswith`` would admit.

    Fails closed: returns False for relative inputs, traversal, prefix
    lookalikes, and any unexpected exception.
    """
    if not os.path.isabs(target) or not os.path.isabs(sandbox_root):
        return False
    try:
        norm_root = os.path.normpath(sandbox_root)
        norm_target = os.path.normpath(target)
        return os.path.commonpath([norm_root, norm_target]) == norm_root
    except (ValueError, OSError):
        return False