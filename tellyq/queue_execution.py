"""Durable dispatch boundaries for one exclusive playback owner.

This service neither selects queue items nor evaluates playback evidence. Its
caller supplies a bounded operation with fresh receiver ownership guards. Never
run recovery concurrently with an owner that can still invoke device effects.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from .domain.ports import Clock, RevisionConflict
from .domain.queue import ExecutionState, JournalCommand, QueueSnapshot, QueueStore
from .domain.values import CommandAction, CommandOutcome, CommandReceipt


@dataclass(frozen=True, slots=True)
class QueueCommandRequest:
    queue_id: str
    item_id: str
    attempt_id: str
    command_id: str
    action: CommandAction
    expected_revision: int
    expected_generation: int

    def __post_init__(self) -> None:
        for identity in (self.queue_id, self.item_id, self.attempt_id, self.command_id):
            if not isinstance(identity, str) or not identity.strip():
                raise ValueError("queue command identities must be nonempty strings")
        if not isinstance(self.action, CommandAction) or self.action not in {
            CommandAction.START,
            CommandAction.STOP,
        }:
            raise ValueError("durable execution currently supports start and stop")
        for counter in (self.expected_revision, self.expected_generation):
            if type(counter) is not int or counter < 0:
                raise ValueError("queue command fences must be nonnegative integers")


@dataclass(frozen=True, slots=True)
class QueueDispatch:
    """Committed local claim, not fresh receiver ownership or remote delivery."""

    snapshot: QueueSnapshot
    command: JournalCommand


class ExecutionProblem(StrEnum):
    OPERATION_FAILED = "operation_failed"
    INVALID_RECEIPT = "invalid_receipt"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    CANCELED_BEFORE_OPERATION = "canceled_before_operation"
    GUARD_FAILED = "guard_failed"


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    snapshot: QueueSnapshot
    command: JournalCommand
    operation_invoked: bool
    receipt: CommandReceipt | None = None
    problem: ExecutionProblem | None = None


class OutcomePersistenceError(RuntimeError):
    """An effect may have happened; retain the result and reconcile, never resend.

    ``result.snapshot`` is the last known committed dispatch snapshot. A failing
    write may itself have committed before its response was lost, so callers must
    reload durable state rather than treating this snapshot as current.
    """

    def __init__(self, result: ExecutionResult) -> None:
        self.result = result
        super().__init__("command outcome was not confirmed durable; reconcile before continuing")


def _command(snapshot: QueueSnapshot, command_id: str) -> JournalCommand | None:
    return next((item for item in snapshot.commands if item.command_id == command_id), None)


def _matching(snapshot: QueueSnapshot, request: QueueCommandRequest) -> JournalCommand | None:
    command = _command(snapshot, request.command_id)
    if command is None:
        return None
    attempt = next(item for item in snapshot.attempts if item.attempt_id == command.attempt_id)
    if (
        command.action != request.action
        or command.attempt_id != request.attempt_id
        or attempt.item_id != request.item_id
    ):
        raise ValueError("command identity already describes another intent")
    if command.generation != request.expected_generation:
        raise RevisionConflict("command belongs to an earlier runner generation")
    return command


def _generation(snapshot: QueueSnapshot, expected_generation: int) -> None:
    if snapshot.generation != expected_generation:
        raise RevisionConflict("runner generation changed; reconcile before continuing")


class QueueExecutor:
    """Journal a single operation without holding a database transaction over I/O.

    The store must make mark_dispatched a strict atomic PENDING→DISPATCHED claim.
    A duplicate successful-looking transition is not a valid implementation.
    Exclusive runner ownership covers the final local check→device-call gap.
    """

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
        request: QueueCommandRequest,
        operation: Callable[[QueueDispatch], CommandReceipt],
        *,
        may_start: Callable[[], bool] | None = None,
    ) -> ExecutionResult:
        """Prepare, claim, invoke once, and record receipt independently of evidence.

        Repeating an existing command only reads its history, including pending
        intent left by a crash. Ordinary operation exceptions become UNCERTAIN;
        process interruption can leave DISPATCHED for explicit owner recovery.
        Pre-dispatch errors propagate without invoking the operation.
        """
        before = self._load(request.queue_id)
        _generation(before, request.expected_generation)
        existing = _matching(before, request)
        if existing is not None:
            return self._historical(before, existing)
        if before.revision != request.expected_revision:
            raise RevisionConflict("queue revision changed; reload before making a decision")
        if request.action == CommandAction.START:
            prepared = self.store.prepare_start(
                request.queue_id,
                request.item_id,
                attempt_id=request.attempt_id,
                command_id=request.command_id,
                at=self.clock.utcnow(),
                expected_revision=request.expected_revision,
            )
        else:
            if not any(
                attempt.attempt_id == request.attempt_id and attempt.item_id == request.item_id
                for attempt in before.attempts
            ):
                raise ValueError("stop requires the selected attempt's item")
            prepared = self.store.prepare_stop(
                request.queue_id,
                attempt_id=request.attempt_id,
                command_id=request.command_id,
                at=self.clock.utcnow(),
                expected_revision=request.expected_revision,
            )
        _generation(prepared, request.expected_generation)
        command = _matching(prepared, request)
        assert command is not None
        if command.state != ExecutionState.PENDING:
            return self._historical(prepared, command)
        if prepared.revision != request.expected_revision + 1:
            raise RevisionConflict("queue changed during command preparation")
        dispatched = self.store.mark_dispatched(
            request.queue_id,
            request.command_id,
            at=self.clock.utcnow(),
            expected_revision=prepared.revision,
        )
        # A concurrent cancellation/recovery after the claim is a conservative
        # hold, even when this invocation has not yet entered the device call.
        current = self._load(request.queue_id)
        _generation(current, request.expected_generation)
        if current.revision != dispatched.revision or (
            request.action == CommandAction.START and current.cancellation_requested
        ):
            raise RevisionConflict("queue changed before device invocation")
        command = _matching(dispatched, request)
        assert command is not None
        dispatch = QueueDispatch(dispatched, command)
        if request.action == CommandAction.START and may_start is not None:
            try:
                allowed = may_start()
            except Exception:
                guard_problem = ExecutionProblem.GUARD_FAILED
            else:
                guard_problem = (
                    None if allowed is True else ExecutionProblem.CANCELED_BEFORE_OPERATION
                )
            if guard_problem is not None:
                return self._record(
                    ExecutionResult(dispatched, command, False, problem=guard_problem),
                    ExecutionState.UNCERTAIN,
                )
        receipt = None
        problem = None
        outcome = ExecutionState.UNCERTAIN
        try:
            received = operation(dispatch)
        except Exception:
            problem = ExecutionProblem.OPERATION_FAILED
        else:
            if (
                not isinstance(received, CommandReceipt)
                or received.request_id != request.command_id
                or received.attempt_id != request.attempt_id
                or received.action != request.action
                or not isinstance(received.outcome, CommandOutcome)
            ):
                problem = ExecutionProblem.INVALID_RECEIPT
            else:
                receipt = received
                outcome = {
                    CommandOutcome.ACCEPTED: ExecutionState.ACKNOWLEDGED,
                    CommandOutcome.REJECTED: ExecutionState.REJECTED,
                    CommandOutcome.UNKNOWN: ExecutionState.UNCERTAIN,
                }[receipt.outcome]
        result = ExecutionResult(dispatched, command, True, receipt, problem)
        return self._record(result, outcome)

    @staticmethod
    def _historical(snapshot: QueueSnapshot, command: JournalCommand) -> ExecutionResult:
        uncertain = command.state in {
            ExecutionState.PENDING,
            ExecutionState.DISPATCHED,
            ExecutionState.UNCERTAIN,
        }
        return ExecutionResult(
            snapshot,
            command,
            False,
            problem=ExecutionProblem.RECONCILIATION_REQUIRED if uncertain else None,
        )

    def _record(self, result: ExecutionResult, outcome: ExecutionState) -> ExecutionResult:
        try:
            recorded = self.store.record_outcome(
                result.snapshot.queue_id,
                result.command.command_id,
                outcome,
                at=self.clock.utcnow(),
                expected_revision=result.snapshot.revision,
            )
        except Exception as error:
            raise OutcomePersistenceError(result) from error
        recorded_command = _command(recorded, result.command.command_id)
        assert recorded_command is not None
        return ExecutionResult(
            recorded, recorded_command, result.operation_invoked, result.receipt, result.problem
        )

    def recover(
        self, queue_id: str, *, expected_revision: int, expected_generation: int
    ) -> QueueSnapshot:
        """Exclusive new owner only: hold uncertain work without replaying effects.

        Acquiring ownership and ensuring the former owner cannot invoke another
        effect are prerequisites. No monotonic playback evidence is restored.
        """
        before = self._load(queue_id)
        _generation(before, expected_generation)
        return self.store.recover(queue_id, expected_revision=expected_revision)
