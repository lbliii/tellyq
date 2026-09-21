"""Durable queue intent and command history, independent of live playback evidence."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from .values import CommandAction, ContentRef, PlaybackTarget


class QueueIntent(StrEnum):
    PENDING = "pending"
    CURRENT = "current"
    FINISHED = "finished"
    SKIPPED = "skipped"
    STOPPED = "stopped"
    NEEDS_ATTENTION = "needs_attention"


class ExecutionState(StrEnum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    ACKNOWLEDGED = "acknowledged"
    REJECTED = "rejected"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class QueueEntry:
    item_id: str
    content: ContentRef
    intent: QueueIntent = QueueIntent.PENDING

    def __post_init__(self) -> None:
        if not self.item_id:
            raise ValueError("queue item identity is required")


@dataclass(frozen=True, slots=True)
class QueueAttempt:
    attempt_id: str
    item_id: str
    intent: QueueIntent
    created_at: datetime
    decision_id: str | None = None


@dataclass(frozen=True, slots=True)
class JournalCommand:
    command_id: str
    attempt_id: str
    action: CommandAction
    state: ExecutionState
    generation: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    queue_id: str
    target: PlaybackTarget
    revision: int
    generation: int
    current_item_id: str | None
    cancellation_requested: bool
    items: tuple[QueueEntry, ...]
    attempts: tuple[QueueAttempt, ...] = ()
    commands: tuple[JournalCommand, ...] = ()


class QueueStore(Protocol):
    """Transactions end before device I/O; this port never dispatches effects.

    An exclusive runner owner must call recover before using a reopened queue.
    Readers may open and load without changing another owner's in-flight state.
    Stable command/decision identities make retries idempotent locally, not on TV.
    """

    def create(
        self, queue_id: str, target: PlaybackTarget, items: tuple[QueueEntry, ...]
    ) -> QueueSnapshot: ...

    def load(self, queue_id: str) -> QueueSnapshot | None: ...

    def prepare_start(
        self,
        queue_id: str,
        item_id: str,
        *,
        attempt_id: str,
        command_id: str,
        at: datetime,
        expected_revision: int,
    ) -> QueueSnapshot: ...

    def prepare_stop(
        self,
        queue_id: str,
        *,
        attempt_id: str,
        command_id: str,
        at: datetime,
        expected_revision: int,
    ) -> QueueSnapshot: ...

    def cancel_queue(self, queue_id: str, *, expected_revision: int) -> QueueSnapshot:
        """Cancel local pending starts, including between items; imply no remote stop."""
        ...

    def mark_dispatched(
        self, queue_id: str, command_id: str, *, at: datetime, expected_revision: int
    ) -> QueueSnapshot:
        """Atomically claim PENDING once; repeated DISPATCHED must fail, not succeed.

        Revision and generation must still match, and canceled starts must fail.
        The returned committed claim permits one exclusive owner's invocation;
        it is not a transferable remote-delivery guarantee.
        """
        ...

    def record_outcome(
        self,
        queue_id: str,
        command_id: str,
        outcome: ExecutionState,
        *,
        at: datetime,
        expected_revision: int,
    ) -> QueueSnapshot: ...

    def settle_attempt(
        self,
        queue_id: str,
        attempt_id: str,
        outcome: QueueIntent,
        *,
        decision_id: str,
        expected_revision: int,
    ) -> QueueSnapshot:
        """Record a caller's evidence decision, never infer it or select a next item."""
        ...

    def recover(self, queue_id: str, *, expected_revision: int) -> QueueSnapshot: ...
