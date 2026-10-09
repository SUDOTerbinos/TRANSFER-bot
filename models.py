"""Shared domain models and state values."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class OrderStatus(StrEnum):
    PENDING = "Pending"
    VALIDATING = "Validating"
    QUEUED = "Queued"
    IN_PROGRESS = "In Progress"
    PAUSED = "Paused"
    COMPLETED = "Completed"
    CANCELLED = "Cancelled"
    NEEDS_REVIEW = "Needs Review"


class WorkerStatus(StrEnum):
    ACTIVE = "Active"
    IDLE = "Idle"
    PROCESSING = "Processing"
    COOLDOWN = "Cooldown"
    PAUSED = "Paused"
    NEEDS_ATTENTION = "Needs Attention"
    DISABLED = "Disabled"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    ASSIGNED = "assigned"
    COMPLETED = "completed"
    PAUSED = "paused"
    NEEDS_REVIEW = "needs_review"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class GroupInfo:
    chat_id: int
    title: str
    username: str | None
    chat_type: str


@dataclass(frozen=True)
class QuotaSnapshot:
    limit: int
    window_seconds: int
    used: int
    remaining: int
    next_eligible_at: str | None


@dataclass(frozen=True)
class AssignmentResult:
    assigned: bool
    worker: dict | None = None
    campaign: dict | None = None
    order: dict | None = None
    task: dict | None = None
    reason: str | None = None
    next_eligible_at: str | None = None


class DomainError(Exception):
    """An expected business-rule failure safe to show to a user."""


class GroupValidationError(DomainError):
    """The Bot API could not verify an order's group or permissions."""


class QuotaExceeded(DomainError):
    """A worker has no remaining action quota in the active reporting window."""


class ActionAlreadyHandled(DomainError):
    """A campaign announcement was already reserved or completed."""
