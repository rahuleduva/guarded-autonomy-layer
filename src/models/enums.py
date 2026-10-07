"""Shared vocabulary for API contracts, policy evaluation, and database models."""

from enum import Enum


class RiskLevel(str, Enum):
    READ_ONLY = "READ_ONLY"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class AutonomyLevel(str, Enum):
    SHADOW = "SHADOW"
    ASSISTED = "ASSISTED"
    LIVE = "LIVE"


class DecisionOutcome(str, Enum):
    ALLOWED = "ALLOWED"
    DENIED = "DENIED"
    ESCALATED = "ESCALATED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    SHADOW_LOGGED = "SHADOW_LOGGED"


class EscalationStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
