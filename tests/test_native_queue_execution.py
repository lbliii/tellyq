"""Real SQLite intent/claim/outcome boundaries around synthetic native effects."""

from dataclasses import replace

import pytest

from tellyq.domain.native_queue import NativeQueueAction
from tellyq.domain.ports import RevisionConflict
from tellyq.domain.queue import ExecutionState, QueueIntent
from tellyq.domain.values import CommandOutcome
from tellyq.native_queue_execution import (
    NativeExecutionRequest,
    NativeOutcomePersistenceError,
    NativeQueueExecutor,
)
from tellyq.queue_execution import ExecutionProblem
from tellyq.queue_store import QueueStoreError, SQLiteQueueStore

from .native_queue_support import NOW, Clock, clear, load, receipt, request, reserve, started

REQUEST = NativeExecutionRequest("queue", "reservation", request(), 3, 0, "b", "attempt-b")


class PowerLoss(BaseException):
    pass


@pytest.fixture
def store(tmp_path):
    result = SQLiteQueueStore(tmp_path / "runtime")
    started(result)
    return result


def forbidden(_dispatch):
    raise AssertionError("historical or fenced native intent must not reach the receiver")


@pytest.mark.parametrize(
    ("outcome", "state"),
    (
        (CommandOutcome.ACCEPTED, ExecutionState.ACKNOWLEDGED),
        (CommandOutcome.REJECTED, ExecutionState.REJECTED),
        (CommandOutcome.UNKNOWN, ExecutionState.UNCERTAIN),
    ),
)
def test_intent_and_claim_commit_before_call_and_receipt_never_proves_playback(
    store, outcome, state
):
    calls = []

    def operation(dispatch):
        assert load(SQLiteQueueStore(store.path.parent)) == dispatch.snapshot
        assert dispatch.operation.state == ExecutionState.DISPATCHED
        assert dispatch.reservation.successor_item_id == "b"
        assert dispatch.request == REQUEST.request
        calls.append(dispatch.operation.operation_id)
        return receipt(dispatch, outcome)

    first = NativeQueueExecutor(store, Clock()).execute(REQUEST, operation)
    assert calls == ["enqueue"] and first.operation_invoked
    assert first.operation.state == state and first.problem is None
    assert first.snapshot.current_item_id == "a"
    assert first.snapshot.items[1].intent == QueueIntent.PENDING
    assert len(first.snapshot.attempts) == len(first.snapshot.commands) == 1
    assert first.snapshot.reconciliation_required == (state == ExecutionState.UNCERTAIN)
    repeated = NativeQueueExecutor(SQLiteQueueStore(store.path.parent), Clock()).execute(
        REQUEST, forbidden
    )
    assert repeated.snapshot == first.snapshot and not repeated.operation_invoked
    assert repeated.receipt is None
    assert repeated.problem == (
        ExecutionProblem.RECONCILIATION_REQUIRED if state == ExecutionState.UNCERTAIN else None
    )


@pytest.mark.parametrize(
    "change",
    (
        {"reservation_id": "other"},
        {"successor_item_id": "c"},
        {"successor_attempt_id": "other"},
        {"request": replace(request(), playback_id="other")},
        {
            "request": replace(
                request(), scope=replace(request().scope, connection_generation="other")
            )
        },
    ),
)
def test_operation_identity_cannot_change_meaning(store, change):
    NativeQueueExecutor(store, Clock()).execute(REQUEST, receipt)
    with pytest.raises((QueueStoreError, RevisionConflict)):
        NativeQueueExecutor(store, Clock()).execute(replace(REQUEST, **change), forbidden)


@pytest.mark.parametrize(
    ("method", "after", "state", "calls"),
    (
        ("prepare_native_successor", False, None, 0),
        ("prepare_native_successor", True, ExecutionState.PENDING, 0),
        ("mark_native_dispatched", False, ExecutionState.PENDING, 0),
        ("mark_native_dispatched", True, ExecutionState.DISPATCHED, 0),
        ("record_native_outcome", False, ExecutionState.DISPATCHED, 1),
        ("record_native_outcome", True, ExecutionState.ACKNOWLEDGED, 1),
    ),
)
def test_process_loss_never_replays_a_durable_intent(
    store, monkeypatch, method, after, state, calls
):
    invoked = []
    real = getattr(store, method)

    def interrupted(*args, **kwargs):
        if after:
            real(*args, **kwargs)
        raise PowerLoss

    def operation(dispatch):
        invoked.append(dispatch.operation.operation_id)
        return receipt(dispatch)

    monkeypatch.setattr(store, method, interrupted)
    with pytest.raises(PowerLoss):
        NativeQueueExecutor(store, Clock()).execute(REQUEST, operation)
    assert len(invoked) == calls
    reopened = SQLiteQueueStore(store.path.parent)
    before = load(reopened)
    assert (before.native_operations[0].state if before.native_operations else None) == state
    if state is not None:
        historical = NativeQueueExecutor(reopened, Clock()).execute(REQUEST, forbidden)
        assert not historical.operation_invoked
        recovered = reopened.recover("queue", expected_revision=before.revision)
        with pytest.raises(RevisionConflict):
            NativeQueueExecutor(reopened, Clock()).execute(
                replace(REQUEST, expected_revision=recovered.revision), forbidden
            )


def test_process_loss_inside_remote_call_remains_dispatched_then_uncertain(store):
    def interrupted(_dispatch):
        raise PowerLoss

    with pytest.raises(PowerLoss):
        NativeQueueExecutor(store, Clock()).execute(REQUEST, interrupted)
    before = load(store)
    assert before.native_operations[0].state == ExecutionState.DISPATCHED
    recovered = store.recover("queue", expected_revision=before.revision)
    assert recovered.native_operations[0].state == ExecutionState.UNCERTAIN
    assert recovered.reconciliation_required


def test_ordinary_failure_is_uncertain_and_never_retried(store):
    def failed(_dispatch):
        raise RuntimeError("private transport details")

    result = NativeQueueExecutor(store, Clock()).execute(REQUEST, failed)
    assert result.operation_invoked and result.problem == ExecutionProblem.OPERATION_FAILED
    assert result.operation.state == ExecutionState.UNCERTAIN
    assert "private transport details" not in repr(result)
    assert not NativeQueueExecutor(store, Clock()).execute(REQUEST, forbidden).operation_invoked


@pytest.mark.parametrize(
    "mismatch", ("operation_id", "playback_id", "scope", "successor", "outcome", "type")
)
def test_full_receipt_correlation_is_required(store, mismatch):
    def mismatched(dispatch):
        original = receipt(dispatch)
        if mismatch == "type":
            return None
        if mismatch == "outcome":
            return replace(original, outcome="accepted")
        if mismatch == "scope":
            return replace(
                original,
                request=replace(
                    dispatch.request, scope=replace(dispatch.request.scope, started_monotonic=9)
                ),
            )
        if mismatch == "successor":
            return replace(
                original,
                request=replace(dispatch.request, successor=dispatch.request.scope.request.content),
            )
        return replace(original, request=replace(dispatch.request, **{mismatch: "other"}))

    result = NativeQueueExecutor(store, Clock()).execute(REQUEST, mismatched)
    assert result.receipt is None and result.operation.state == ExecutionState.UNCERTAIN
    assert result.problem == ExecutionProblem.INVALID_RECEIPT


@pytest.mark.parametrize("guard", (False, None, 1, "raise"))
def test_guard_failure_records_uncertainty_without_effect(store, guard):
    def may_effect():
        if guard == "raise":
            raise RuntimeError("guard failed")
        return guard

    result = NativeQueueExecutor(store, Clock()).execute(REQUEST, forbidden, may_effect=may_effect)
    assert not result.operation_invoked and result.operation.state == ExecutionState.UNCERTAIN
    assert result.problem == (
        ExecutionProblem.GUARD_FAILED
        if guard == "raise"
        else ExecutionProblem.CANCELED_BEFORE_OPERATION
    )


@pytest.mark.parametrize("mutation", ("cancel", "hold", "recover"))
def test_post_claim_final_reread_blocks_concurrent_mutation(store, monkeypatch, mutation):
    original = store.mark_native_dispatched

    def claimed(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        other = SQLiteQueueStore(store.path.parent)
        if mutation == "cancel":
            other.cancel_queue("queue", expected_revision=snapshot.revision)
        elif mutation == "hold":
            other.hold_for_reconciliation("queue", expected_revision=snapshot.revision)
        else:
            other.recover("queue", expected_revision=snapshot.revision)
        return snapshot

    monkeypatch.setattr(store, "mark_native_dispatched", claimed)
    with pytest.raises(RevisionConflict, match="before native invocation"):
        NativeQueueExecutor(store, Clock()).execute(REQUEST, forbidden)


@pytest.mark.parametrize("after", (False, True))
def test_failed_outcome_persistence_retains_result_without_resend(store, monkeypatch, after):
    real = store.record_native_outcome

    def interrupted(*args, **kwargs):
        if after:
            real(*args, **kwargs)
        raise OSError("write response lost")

    monkeypatch.setattr(store, "record_native_outcome", interrupted)
    with pytest.raises(NativeOutcomePersistenceError) as caught:
        NativeQueueExecutor(store, Clock()).execute(REQUEST, receipt)
    assert caught.value.result.operation_invoked
    assert caught.value.result.receipt is not None
    assert caught.value.result.operation.state == ExecutionState.DISPATCHED
    reopened = SQLiteQueueStore(store.path.parent)
    result = NativeQueueExecutor(reopened, Clock()).execute(REQUEST, forbidden)
    assert not result.operation_invoked
    assert result.operation.state == (
        ExecutionState.ACKNOWLEDGED if after else ExecutionState.DISPATCHED
    )


def test_prepared_intent_after_crash_is_history_even_if_revision_is_reloaded(store):
    prepared = reserve(store)
    result = NativeQueueExecutor(store, Clock()).execute(
        replace(REQUEST, expected_revision=prepared.revision), forbidden
    )
    assert not result.operation_invoked and result.operation.state == ExecutionState.PENDING
    assert result.problem == ExecutionProblem.RECONCILIATION_REQUIRED


def test_explicit_clear_is_independent_of_uncertain_enqueue_and_is_not_repeated(store):
    first = NativeQueueExecutor(store, Clock()).execute(
        REQUEST, lambda dispatch: receipt(dispatch, CommandOutcome.UNKNOWN)
    )
    canceled = store.cancel_queue("queue", expected_revision=first.snapshot.revision)
    clear_request = NativeExecutionRequest("queue", "reservation", clear(), canceled.revision, 0)
    result = NativeQueueExecutor(store, Clock()).execute(
        clear_request, lambda dispatch: receipt(dispatch, CommandOutcome.UNKNOWN)
    )
    assert result.operation.action == NativeQueueAction.CLEAR
    assert result.operation.state == ExecutionState.UNCERTAIN and result.operation_invoked
    assert result.reservation.exit_decision_id is None
    assert (
        not NativeQueueExecutor(store, Clock()).execute(clear_request, forbidden).operation_invoked
    )
    # Unknown clear does not prevent a separate owned STOP from being journaled.
    stopped = store.prepare_stop(
        "queue",
        attempt_id="attempt-a",
        command_id="stop-a",
        at=NOW,
        expected_revision=result.snapshot.revision,
    )
    assert stopped.commands[-1].command_id == "stop-a"


@pytest.mark.parametrize(
    "change",
    (
        {"queue_id": ""},
        {"reservation_id": ""},
        {"expected_revision": True},
        {"expected_generation": -1},
        {"successor_attempt_id": None},
        {"successor_item_id": ""},
        {"request": clear()},
    ),
)
def test_invalid_execution_requests_fail_before_store_access(change):
    with pytest.raises(ValueError):
        replace(REQUEST, **change)
