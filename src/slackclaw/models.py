from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TaskStatus(str, Enum):
    PENDING = "pending"
    WAITING_APPROVAL = "waiting_approval"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    ABORTED_ON_RESTART = "aborted_on_restart"


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    status: TaskStatus
    payload: dict
    created_at: str
    updated_at: str


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True)
class SlackMessage:
    channel_id: str
    ts: str
    user: str
    text: str
    raw: dict


@dataclass(frozen=True)
class SlackReaction:
    channel_id: str
    message_ts: str
    reaction: str
    user: str
    raw: dict


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    channel_id: str
    message_ts: str
    trigger_user: str
    trigger_text: str
    command_text: str
    lock_key: str


@dataclass(frozen=True)
class TaskExecutionResult:
    status: TaskStatus
    summary: str
    details: str


@dataclass(frozen=True)
class TaskApprovalRecord:
    task_id: str
    channel_id: str
    source_message_ts: str
    approval_message_ts: str
    approve_reaction: str
    reject_reaction: str
    status: ApprovalStatus
    decided_by: str
    decision_reaction: str
    created_at: str
    updated_at: str


TERMINAL_TASK_STATUSES = {
    TaskStatus.SUCCEEDED,
    TaskStatus.FAILED,
    TaskStatus.CANCELED,
    TaskStatus.ABORTED_ON_RESTART,
}
