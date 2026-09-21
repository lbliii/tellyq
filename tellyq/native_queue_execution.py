"""Once-only native queue effects with durable intent and explicit uncertainty.

The exclusive foreground owner supplies fresh scope guards. No database
transaction spans receiver I/O, and neither a receipt nor a reservation proves
remote queue membership or playback. Recovery never replays native operations.
"""

from collections.abc import Callable
from dataclasses import dataclass

from .domain.native_queue import NativeQueueAction, NativeQueueReceipt, NativeQueueRequest
from .domain.ports import Clock, RevisionConflict
from .domain.queue import (
    ExecutionState,
    NativeOperation,
    NativeReservation,
    QueueSnapshot,
    QueueStore,
)
from .domain.values import CommandOutcome
from .queue_execution import ExecutionProblem


@dataclass(frozen=True, slots=True)
class NativeExecutionRequest:
    queue_id: str
    reservation_id: str
    request: NativeQueueRequest
    expected_revision: int
    expected_generation: int
    successor_item_id: str | None = None
    successor_attempt_id: str | None = None

    def __post_init__(self) -> None:
        for identity in (self.queue_id, self.reservation_id):
            if not isinstance(identity, str) or not identity.strip():
                raise ValueError("native execution identities must be nonempty strings")
        if not isinstance(self.request, NativeQueueRequest):
            raise ValueError("native execution requires a native request")
        successor = (self.successor_item_id, self.successor_attempt_id)
        if self.request.action == NativeQueueAction.PLAY_NEXT:
            if any(not isinstance(value, str) or not value.strip() for value in successor):
                raise ValueError("play-next requires successor item and attempt identities")
        elif successor != (None, None):
            raise ValueError("clear cannot supply successor identities")
        for value in (self.expected_revision, self.expected_generation):
            if type(value) is not int or value < 0:
                raise ValueError("native execution fences must be nonnegative integers")


@dataclass(frozen=True, slots=True)
class NativeDispatch:
    snapshot: QueueSnapshot
    reservation: NativeReservation
    operation: NativeOperation
    request: NativeQueueRequest


@dataclass(frozen=True, slots=True)
class NativeExecutionResult:
    snapshot: QueueSnapshot
    reservation: NativeReservation
    operation: NativeOperation
    operation_invoked: bool
    receipt: NativeQueueReceipt | None = None
    problem: ExecutionProblem | None = None


class NativeOutcomePersistenceError(RuntimeError):
    """An effect may have occurred; reload and reconcile, never resend.

    The retained snapshot is the last known dispatch commit, not necessarily
    current durable state: the failed outcome response may hide a committed write.
    """

    def __init__(self, result: NativeExecutionResult) -> None:
        self.result = result
        super().__init__("native outcome was not confirmed durable; reconcile before continuing")


def _records(
    snapshot: QueueSnapshot, request: NativeExecutionRequest
) -> tuple[NativeReservation, NativeOperation]:
    return (
        next(r for r in snapshot.reservations if r.reservation_id == request.reservation_id),
        next(
            o for o in snapshot.native_operations if o.operation_id == request.request.operation_id
        ),
    )


class NativeQueueExecutor:
    """Prepare and claim once before invoking one caller-guarded native effect."""

    def __init__(self, store: QueueStore, clock: Clock) -> None:
        self.store = store
        self.clock = clock

    def _load(self, queue_id: str) -> QueueSnapshot:
        snapshot = self.store.load(queue_id)
        if snapshot is None:
            raise ValueError("queue does not exist")
        return snapshot

    def execute(
        self,
        request: NativeExecutionRequest,
        operation: Callable[[NativeDispatch], NativeQueueReceipt],
        *,
        may_effect: Callable[[], bool] | None = None,
    ) -> NativeExecutionResult:
        """Duplicate IDs only return history, even when the old intent is pending.

        Ordinary operation errors are uncertain. Process interruptions retain a
        dispatched claim for explicit recovery. A fresh CLEAR is independent of
        uncertain PLAY_NEXT; ordinary owned STOP remains a separate executor call.
        """
        before = self._load(request.queue_id)
        if before.generation != request.expected_generation:
            raise RevisionConflict("native owner generation changed")
        existing = any(
            o.operation_id == request.request.operation_id for o in before.native_operations
        )
        if request.request.action == NativeQueueAction.PLAY_NEXT:
            assert request.successor_item_id is not None
            assert request.successor_attempt_id is not None
            prepared = self.store.prepare_native_successor(
                request.queue_id,
                request.successor_item_id,
                reservation_id=request.reservation_id,
                successor_attempt_id=request.successor_attempt_id,
                request=request.request,
                at=self.clock.utcnow(),
                expected_revision=request.expected_revision,
                expected_generation=request.expected_generation,
            )
        else:
            prepared = self.store.prepare_native_clear(
                request.queue_id,
                request.reservation_id,
                request=request.request,
                at=self.clock.utcnow(),
                expected_revision=request.expected_revision,
                expected_generation=request.expected_generation,
            )
        reservation, journal = _records(prepared, request)
        if existing or journal.state != ExecutionState.PENDING:
            uncertain = journal.state in {
                ExecutionState.PENDING,
                ExecutionState.DISPATCHED,
                ExecutionState.UNCERTAIN,
            }
            return NativeExecutionResult(
                prepared,
                reservation,
                journal,
                False,
                problem=ExecutionProblem.RECONCILIATION_REQUIRED if uncertain else None,
            )
        if prepared.revision != request.expected_revision + 1:
            raise RevisionConflict("queue changed during native preparation")
        dispatched = self.store.mark_native_dispatched(
            request.queue_id,
            request.request.operation_id,
            at=self.clock.utcnow(),
            expected_revision=prepared.revision,
            expected_generation=request.expected_generation,
        )
        current = self._load(request.queue_id)
        if (
            current.generation != request.expected_generation
            or current.revision != dispatched.revision
        ):
            raise RevisionConflict("queue changed before native invocation")
        reservation, journal = _records(dispatched, request)
        if may_effect is not None:
            try:
                allowed = may_effect()
            except Exception:
                problem = ExecutionProblem.GUARD_FAILED
            else:
                problem = None if allowed is True else ExecutionProblem.CANCELED_BEFORE_OPERATION
            if problem is not None:
                return self._record(
                    NativeExecutionResult(dispatched, reservation, journal, False, problem=problem),
                    ExecutionState.UNCERTAIN,
                )
        receipt = None
        problem = None
        outcome = ExecutionState.UNCERTAIN
        try:
            received = operation(NativeDispatch(dispatched, reservation, journal, request.request))
        except Exception:
            problem = ExecutionProblem.OPERATION_FAILED
        else:
            if (
                not isinstance(received, NativeQueueReceipt)
                or received.request != request.request
                or not isinstance(received.outcome, CommandOutcome)
            ):
                problem = ExecutionProblem.INVALID_RECEIPT
            else:
                receipt = received
                outcome = {
                    CommandOutcome.ACCEPTED: ExecutionState.ACKNOWLEDGED,
                    CommandOutcome.REJECTED: ExecutionState.REJECTED,
                    CommandOutcome.UNKNOWN: ExecutionState.UNCERTAIN,
                }[received.outcome]
        return self._record(
            NativeExecutionResult(dispatched, reservation, journal, True, receipt, problem), outcome
        )

    def _record(
        self, result: NativeExecutionResult, outcome: ExecutionState
    ) -> NativeExecutionResult:
        try:
            snapshot = self.store.record_native_outcome(
                result.snapshot.queue_id,
                result.operation.operation_id,
                outcome,
                at=self.clock.utcnow(),
                expected_revision=result.snapshot.revision,
                expected_generation=result.snapshot.generation,
            )
        except Exception as error:
            raise NativeOutcomePersistenceError(result) from error
        journal = next(
            o for o in snapshot.native_operations if o.operation_id == result.operation.operation_id
        )
        reservation = next(
            r
            for r in snapshot.reservations
            if r.reservation_id == result.reservation.reservation_id
        )
        return NativeExecutionResult(
            snapshot, reservation, journal, result.operation_invoked, result.receipt, result.problem
        )
