"""Durable queue intent and command history, independent of live playback evidence."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from .native_queue import NativeQueueAction, NativeQueueRequest
from .values import CommandAction, ContentRef, PlaybackTarget, SessionSnapshot


class QueueMode(StrEnum):
    LEGACY = "legacy"
    NATIVE = "native"


class AttemptOrigin(StrEnum):
    LOCAL_START = "local_start"
    NATIVE_OBSERVED = "native_observed"


class NativeReservationState(StrEnum):
    RESERVED = "reserved"
    ADOPTED = "adopted"
    SESSION_EXIT_OBSERVED = "session_exit_observed"


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
    origin: AttemptOrigin = AttemptOrigin.LOCAL_START
    reservation_id: str | None = None


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
class NativeReservation:
    reservation_id: str
    predecessor_attempt_id: str
    successor_item_id: str
    successor_attempt_id: str
    application_id: str
    session_id: str
    predecessor_playback_id: str
    generation: int
    created_at: datetime
    state: NativeReservationState = NativeReservationState.RESERVED
    adoption_decision_id: str | None = None
    exit_decision_id: str | None = None


@dataclass(frozen=True, slots=True)
class NativeOperation:
    operation_id: str
    reservation_id: str
    action: NativeQueueAction
    state: ExecutionState
    generation: int
    created_at: datetime
    updated_at: datetime
    scope_attempt_id: str
    connection_generation: str
    playback_id: str


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
    mode: QueueMode = QueueMode.LEGACY
    reconciliation_required: bool = False
    reservations: tuple[NativeReservation, ...] = ()
    native_operations: tuple[NativeOperation, ...] = ()


class QueueStore(Protocol):
    """Transactions end before device I/O; this port never dispatches effects.

    An exclusive runner owner must call recover before using a reopened queue.
    Readers may open and load without changing another owner's in-flight state.
    Stable command/decision identities make retries idempotent locally, not on TV.
    """

    def create(
        self,
        queue_id: str,
        target: PlaybackTarget,
        items: tuple[QueueEntry, ...],
        *,
        mode: QueueMode = QueueMode.LEGACY,
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

    def prepare_control(
        self,
        queue_id: str,
        *,
        attempt_id: str,
        command_id: str,
        action: CommandAction,
        at: datetime,
        expected_revision: int,
    ) -> QueueSnapshot: ...

    def prepare_release(
        self,
        queue_id: str,
        *,
        attempt_id: str,
        command_id: str,
        at: datetime,
        expected_revision: int,
    ) -> QueueSnapshot:
        """Prepare at most one release of the latest finished attempt; never cancel."""
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

    def hold_for_reconciliation(self, queue_id: str, *, expected_revision: int) -> QueueSnapshot:
        """Prevent new native staging without claiming cancellation of receiver work."""
        ...

    def prepare_native_successor(
        self,
        queue_id: str,
        successor_item_id: str,
        *,
        reservation_id: str,
        successor_attempt_id: str,
        request: NativeQueueRequest,
        at: datetime,
        expected_revision: int,
        expected_generation: int,
    ) -> QueueSnapshot: ...

    def prepare_native_clear(
        self,
        queue_id: str,
        reservation_id: str,
        *,
        request: NativeQueueRequest,
        at: datetime,
        expected_revision: int,
        expected_generation: int,
    ) -> QueueSnapshot:
        """Require explicit local cancellation; CLEAR return never proves membership."""
        ...

    def mark_native_dispatched(
        self,
        queue_id: str,
        operation_id: str,
        *,
        at: datetime,
        expected_revision: int,
        expected_generation: int,
    ) -> QueueSnapshot: ...

    def record_native_outcome(
        self,
        queue_id: str,
        operation_id: str,
        outcome: ExecutionState,
        *,
        at: datetime,
        expected_revision: int,
        expected_generation: int,
    ) -> QueueSnapshot: ...

    def adopt_native_successor(
        self,
        queue_id: str,
        reservation_id: str,
        *,
        observed: SessionSnapshot,
        now: float,
        at: datetime,
        decision_id: str,
        expected_revision: int,
        expected_generation: int,
    ) -> QueueSnapshot:
        """Persist caller-qualified fresh B progress; never infer predecessor completion.

        An uncompleted predecessor becomes NEEDS_ATTENTION, retaining cancellation
        and setting reconciliation hold. No START command or receipt is invented.
        """
        ...

    def record_native_session_exit(
        self,
        queue_id: str,
        reservation_id: str,
        *,
        at: datetime,
        decision_id: str,
        expected_revision: int,
        expected_generation: int,
    ) -> QueueSnapshot:
        """Record caller-verified owned session exit, never an empty playlist claim.

        Caller must first verify fresh correlated receiver idle after its owned
        stop/release boundary; this store records that decision without device I/O.
        """
        ...
