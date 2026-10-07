"""Database constraints derived from the shared enums.

Migration revisions retain literal values to preserve historical schemas.
"""

from enum import Enum

from sqlalchemy import CheckConstraint


def enum_check(column: str, enum_type: type[Enum], name: str) -> CheckConstraint:
    """Build a named IN constraint for an internal column and string enum."""
    values = ", ".join(
        "'" + str(member.value).replace("'", "''") + "'"
        for member in enum_type
    )
    return CheckConstraint(f"{column} IN ({values})", name=name)
