"""Real SQLite crash boundaries and detached fake effects; all tests stay offline."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from threading import Barrier, Event

import pytest

from tellyq.domain.ports import RevisionConflict
from tellyq.domain.queue import ExecutionState, QueueEntry, QueueIntent
from tellyq.domain.values import (
    CommandAction,
    CommandOutcome,
    CommandReceipt,
    ContentRef,
    PlaybackTarget,
)
from tellyq.queue_execution import (
    ExecutionProblem,
    OutcomePersistenceError,
    QueueCommandRequest,
    QueueExecutor,
)
from tellyq.queue_store import QueueStoreError, SQLiteQueueStore

NOW = datetime(2026, 9, 21, 16, tzinfo=UTC)
REQUEST = QueueCommandRequest("queue", "first", "attempt", "start", CommandAction.START, 0, 0)


class Clock:
    def utcnow(self):
        return NOW

    def monotonic(self):
        raise AssertionError("durable execution must not restore or use monotonic evidence")


class PowerLoss(BaseException):
    pass


@pytest.fixture
def store(tmp_path):
    result = SQLiteQueueStore(tmp_path / "runtime")
    result.create(
        "queue",
        PlaybackTarget("synthetic-device", "cast"),
        (
            QueueEntry("first", ContentRef("youtube", "first-content")),
            QueueEntry("second", ContentRef("other-provider", "second-content")),
        ),
    )
    return result


def receipt(dispatch, outcome=CommandOutcome.ACCEPTED):
    command = dispatch.command
    return CommandReceipt(command.command_id, command.attempt_id, command.action, outcome, NOW)


def reopen(store):
    return SQLiteQueueStore(store.path.parent)


def load(store):
    snapshot = store.load("queue")
    assert snapshot is not None
    return snapshot


def forbidden(_dispatch):
    raise AssertionError("a historical or fenced command must never call the device")


@pytest.mark.parametrize(
    ("outcome", "state"),
    [
        (CommandOutcome.ACCEPTED, ExecutionState.ACKNOWLEDGED),
        (CommandOutcome.REJECTED, ExecutionState.REJECTED),
        (CommandOutcome.UNKNOWN, ExecutionState.UNCERTAIN),
    ],
)
def test_receipt_outcome_is_durable_but_never_playback_or_completion(store, outcome, state):
    observed = []

    def operation(dispatch):
        # A separate connection sees both durable intent and claim before I/O.
        durable = load(reopen(store))
        assert durable == dispatch.snapshot
        assert durable.commands[0].state == ExecutionState.DISPATCHED
        observed.append(dispatch.command.command_id)
        return receipt(dispatch, outcome)

    result = QueueExecutor(store, Clock()).execute(REQUEST, operation)
    assert observed == ["start"]
    assert result.operation_invoked and result.problem is None
    assert result.receipt is not None and result.receipt.outcome == outcome
    assert result.command.state == state
    assert load(reopen(store)) == result.snapshot
    assert result.snapshot.items[0].intent == QueueIntent.CURRENT
    assert result.snapshot.items[1].intent == QueueIntent.PENDING
    assert result.snapshot.attempts[0].decision_id is None
    assert not hasattr(result.snapshot, "monotonic")
    assert not hasattr(result.snapshot, "completion")


@pytest.mark.parametrize("outcome", tuple(CommandOutcome))
def test_repeated_id_returns_history_without_dispatch_even_with_old_revision(store, outcome):
    first = QueueExecutor(store, Clock()).execute(REQUEST, lambda call: receipt(call, outcome))
    repeated = QueueExecutor(reopen(store), Clock()).execute(REQUEST, forbidden)
    assert repeated.snapshot == first.snapshot
    assert repeated.command == first.command
    assert not repeated.operation_invoked
    assert repeated.receipt is None  # No new receipt is fabricated from stored acknowledgement.
    assert repeated.problem == (
        ExecutionProblem.RECONCILIATION_REQUIRED if outcome == CommandOutcome.UNKNOWN else None
    )


@pytest.mark.parametrize(
    "change",
    [
        {"attempt_id": "different"},
        {"item_id": "second"},
        {"action": CommandAction.STOP},
    ],
)
def test_reused_id_cannot_change_its_meaning(store, change):
    first = QueueExecutor(store, Clock()).execute(REQUEST, receipt)
    with pytest.raises(ValueError, match="another intent"):
        QueueExecutor(store, Clock()).execute(replace(REQUEST, **change), forbidden)
    assert load(store) == first.snapshot


@pytest.mark.parametrize(
    ("method", "after", "expected_state", "calls"),
    [
        ("prepare_start", False, None, 0),
        ("prepare_start", True, ExecutionState.PENDING, 0),
        ("mark_dispatched", False, ExecutionState.PENDING, 0),
        ("mark_dispatched", True, ExecutionState.DISPATCHED, 0),
        ("record_outcome", False, ExecutionState.DISPATCHED, 1),
        ("record_outcome", True, ExecutionState.ACKNOWLEDGED, 1),
    ],
)
def test_process_loss_at_each_storage_boundary_never_replays(
    store, monkeypatch, method, after, expected_state, calls
):
    original = getattr(store, method)

    def interrupted(*args, **kwargs):
        if after:
            original(*args, **kwargs)
        raise PowerLoss

    monkeypatch.setattr(store, method, interrupted)
    effects = []

    def operation(dispatch):
        effects.append("effect")
        return receipt(dispatch)

    with pytest.raises(PowerLoss):
        QueueExecutor(store, Clock()).execute(REQUEST, operation)
    fresh = reopen(store)
    snapshot = load(fresh)
    assert len(effects) == calls
    if expected_state is None:
        assert snapshot.commands == () and snapshot.items[0].intent == QueueIntent.PENDING
    else:
        assert snapshot.commands[0].state == expected_state
        historical = QueueExecutor(fresh, Clock()).execute(REQUEST, forbidden)
        assert not historical.operation_invoked
        recovered = QueueExecutor(fresh, Clock()).recover(
            "queue", expected_revision=snapshot.revision, expected_generation=snapshot.generation
        )
        assert recovered.items[0].intent == QueueIntent.NEEDS_ATTENTION
        assert recovered.cancellation_requested
        assert recovered.commands[0].state == (
            ExecutionState.UNCERTAIN
            if expected_state == ExecutionState.DISPATCHED
            else expected_state
        )
        with pytest.raises(RevisionConflict, match="generation"):
            QueueExecutor(fresh, Clock()).execute(REQUEST, forbidden)
        assert load(reopen(store)) == recovered


@pytest.mark.parametrize("after_effect", [False, True])
def test_process_dies_inside_operation_leaving_uncertain_delivery(store, after_effect):
    effects = []

    def operation(_dispatch):
        if after_effect:
            effects.append("effect")
        raise PowerLoss

    with pytest.raises(PowerLoss):
        QueueExecutor(store, Clock()).execute(REQUEST, operation)
    fresh = reopen(store)
    before = load(fresh)
    assert before.commands[0].state == ExecutionState.DISPATCHED
    assert len(effects) == int(after_effect)
    assert QueueExecutor(fresh, Clock()).execute(REQUEST, forbidden).problem == (
        ExecutionProblem.RECONCILIATION_REQUIRED
    )
    after = QueueExecutor(fresh, Clock()).recover(
        "queue", expected_revision=before.revision, expected_generation=before.generation
    )
    assert after.commands[0].state == ExecutionState.UNCERTAIN
    assert after.items[0].intent == QueueIntent.NEEDS_ATTENTION


def test_operation_exception_is_uncertain_without_leaking_exception_text(store):
    def operation(_dispatch):
        raise RuntimeError("private receiver payload")

    result = QueueExecutor(store, Clock()).execute(REQUEST, operation)
    assert result.operation_invoked
    assert result.problem == ExecutionProblem.OPERATION_FAILED
    assert result.receipt is None
    assert result.command.state == ExecutionState.UNCERTAIN
    assert "private receiver payload" not in repr(result)
    assert load(reopen(store)) == result.snapshot
    assert not QueueExecutor(store, Clock()).execute(REQUEST, forbidden).operation_invoked


@pytest.mark.parametrize(
    "change",
    [
        {"request_id": "other"},
        {"attempt_id": "other"},
        {"action": CommandAction.STOP},
        {"outcome": "accepted"},
        None,
    ],
)
def test_mismatched_or_malformed_receipt_never_acknowledges_command(store, change):
    def operation(dispatch):
        return None if change is None else replace(receipt(dispatch), **change)

    result = QueueExecutor(store, Clock()).execute(REQUEST, operation)
    assert result.operation_invoked and result.receipt is None
    assert result.problem == ExecutionProblem.INVALID_RECEIPT
    assert result.command.state == ExecutionState.UNCERTAIN
    assert load(reopen(store)) == result.snapshot


@pytest.mark.parametrize("commit_first", [False, True])
def test_outcome_write_failure_retains_receipt_and_never_resends(store, monkeypatch, commit_first):
    original = store.record_outcome

    def failed_write(*args, **kwargs):
        if commit_first:
            original(*args, **kwargs)
        raise OSError("simulated unavailable storage")

    monkeypatch.setattr(store, "record_outcome", failed_write)
    with pytest.raises(OutcomePersistenceError) as error:
        QueueExecutor(store, Clock()).execute(REQUEST, receipt)
    known = error.value.result
    assert known.operation_invoked and known.command.state == ExecutionState.DISPATCHED
    assert known.receipt is not None and known.receipt.outcome == CommandOutcome.ACCEPTED
    fresh = reopen(store)
    assert load(fresh).commands[0].state == (
        ExecutionState.ACKNOWLEDGED if commit_first else ExecutionState.DISPATCHED
    )
    assert not QueueExecutor(fresh, Clock()).execute(REQUEST, forbidden).operation_invoked


@pytest.mark.parametrize("boundary", ["prepare_start", "mark_dispatched"])
def test_committed_cancellation_before_invocation_blocks_start(store, monkeypatch, boundary):
    original = getattr(store, boundary)

    def cancel_after(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        reopen(store).cancel_queue("queue", expected_revision=snapshot.revision)
        return snapshot

    monkeypatch.setattr(store, boundary, cancel_after)
    with pytest.raises(RevisionConflict):
        QueueExecutor(store, Clock()).execute(REQUEST, forbidden)
    snapshot = load(reopen(store))
    assert snapshot.cancellation_requested
    assert snapshot.commands[0].state == (
        ExecutionState.PENDING if boundary == "prepare_start" else ExecutionState.DISPATCHED
    )


def test_recovery_invalidates_worker_between_claim_and_invocation(store, monkeypatch):
    original = store.mark_dispatched

    def recover_after(*args, **kwargs):
        dispatched = original(*args, **kwargs)
        reopen(store).recover("queue", expected_revision=dispatched.revision)
        return dispatched

    monkeypatch.setattr(store, "mark_dispatched", recover_after)
    with pytest.raises(RevisionConflict, match="generation"):
        QueueExecutor(store, Clock()).execute(REQUEST, forbidden)
    assert load(reopen(store)).commands[0].state == ExecutionState.UNCERTAIN


@pytest.mark.parametrize("failed_guard", [False, True])
def test_final_cancellation_guard_records_conservative_hold_without_effect(store, failed_guard):
    def guard():
        if failed_guard:
            raise RuntimeError("guard unavailable")
        return False

    result = QueueExecutor(store, Clock()).execute(REQUEST, forbidden, may_start=guard)
    assert not result.operation_invoked
    assert result.command.state == ExecutionState.UNCERTAIN
    assert result.problem == (
        ExecutionProblem.GUARD_FAILED
        if failed_guard
        else ExecutionProblem.CANCELED_BEFORE_OPERATION
    )
    assert load(reopen(store)) == result.snapshot
    assert not QueueExecutor(store, Clock()).execute(REQUEST, forbidden).operation_invoked


def test_guard_allows_bounded_operation(store):
    assert (
        QueueExecutor(store, Clock())
        .execute(REQUEST, receipt, may_start=lambda: True)
        .operation_invoked
    )


def test_other_writer_can_cancel_during_effect_without_database_lock(store):
    def operation(dispatch):
        with ThreadPoolExecutor(max_workers=1) as pool:
            canceled = pool.submit(
                reopen(store).cancel_queue, "queue", expected_revision=dispatch.snapshot.revision
            ).result(timeout=2)
        assert canceled.cancellation_requested
        return receipt(dispatch)

    with pytest.raises(OutcomePersistenceError) as error:
        QueueExecutor(store, Clock()).execute(REQUEST, operation)
    assert isinstance(error.value.__cause__, RevisionConflict)
    assert error.value.result.receipt is not None
    snapshot = load(reopen(store))
    assert snapshot.cancellation_requested
    assert snapshot.commands[0].state == ExecutionState.DISPATCHED
    assert snapshot.items[0].intent == QueueIntent.CURRENT


def test_generation_change_during_effect_rejects_late_receipt(store):
    def operation(dispatch):
        reopen(store).recover("queue", expected_revision=dispatch.snapshot.revision)
        return receipt(dispatch)

    with pytest.raises(OutcomePersistenceError) as error:
        QueueExecutor(store, Clock()).execute(REQUEST, operation)
    assert isinstance(error.value.__cause__, RevisionConflict)
    snapshot = load(reopen(store))
    assert snapshot.commands[0].state == ExecutionState.UNCERTAIN
    assert snapshot.items[0].intent == QueueIntent.NEEDS_ATTENTION


def test_stop_commits_cancellation_but_receipt_does_not_settle_attempt(store):
    started = QueueExecutor(store, Clock()).execute(REQUEST, receipt)
    request = replace(
        REQUEST,
        action=CommandAction.STOP,
        command_id="stop",
        expected_revision=started.snapshot.revision,
    )

    def stop(dispatch):
        assert load(reopen(store)).cancellation_requested
        assert dispatch.command.action == CommandAction.STOP
        return receipt(dispatch)

    result = QueueExecutor(store, Clock()).execute(request, stop, may_start=lambda: False)
    assert result.command.state == ExecutionState.ACKNOWLEDGED
    assert result.snapshot.items[0].intent == QueueIntent.CURRENT
    assert result.snapshot.items[1].intent == QueueIntent.PENDING
    assert result.snapshot.attempts[0].decision_id is None
    assert not QueueExecutor(store, Clock()).execute(request, forbidden).operation_invoked


def test_stop_refuses_wrong_item_without_creating_intent(store):
    started = QueueExecutor(store, Clock()).execute(REQUEST, receipt)
    request = replace(
        REQUEST,
        item_id="second",
        action=CommandAction.STOP,
        command_id="stop",
        expected_revision=started.snapshot.revision,
    )
    with pytest.raises(ValueError, match="selected attempt"):
        QueueExecutor(store, Clock()).execute(request, forbidden)
    assert load(store) == started.snapshot


def test_two_racing_same_id_calls_invoke_effect_at_most_once(store, monkeypatch):
    barrier = Barrier(2)
    effects = []
    executors = []
    for _ in range(2):
        independent = reopen(store)
        original = independent.load

        def synchronized_load(queue_id, original=original):
            snapshot = original(queue_id)
            if snapshot is not None and snapshot.revision == 0:
                barrier.wait(timeout=2)
            return snapshot

        monkeypatch.setattr(independent, "load", synchronized_load)
        executors.append(QueueExecutor(independent, Clock()))

    def operation(dispatch):
        effects.append("effect")
        return receipt(dispatch)

    def run(executor):
        try:
            return executor.execute(REQUEST, operation)
        except RevisionConflict, QueueStoreError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, executors))
    assert effects == ["effect"]
    assert sum(item is not None and item.operation_invoked for item in results) == 1
    assert len(load(store).commands) == 1


def test_duplicate_during_inflight_effect_reads_dispatched_without_reinvoking(store):
    entered, release = Event(), Event()

    def operation(dispatch):
        entered.set()
        assert release.wait(timeout=2)
        return receipt(dispatch)

    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(QueueExecutor(store, Clock()).execute, REQUEST, operation)
        try:
            assert entered.wait(timeout=2)
            duplicate = QueueExecutor(reopen(store), Clock()).execute(REQUEST, forbidden)
            assert not duplicate.operation_invoked
            assert duplicate.command.state == ExecutionState.DISPATCHED
            assert duplicate.problem == ExecutionProblem.RECONCILIATION_REQUIRED
        finally:
            release.set()
        assert running.result(timeout=2).command.state == ExecutionState.ACKNOWLEDGED


@pytest.mark.parametrize(
    "change",
    [
        {"command_id": ""},
        {"queue_id": " "},
        {"item_id": ""},
        {"attempt_id": ""},
        {"action": "start"},
        {"action": CommandAction.PAUSE},
        {"action": CommandAction.RESUME},
        {"expected_revision": -1},
        {"expected_generation": -1},
        {"expected_revision": True},
        {"expected_generation": True},
    ],
)
def test_request_rejects_invalid_identity_action_or_fence(change):
    with pytest.raises(ValueError):
        replace(REQUEST, **change)


def test_unknown_queue_and_stale_revision_refuse_without_device_call(store):
    executor = QueueExecutor(store, Clock())
    with pytest.raises(ValueError, match="does not exist"):
        executor.execute(replace(REQUEST, queue_id="unknown"), forbidden)
    with pytest.raises(RevisionConflict, match="revision"):
        executor.execute(replace(REQUEST, expected_revision=1), forbidden)
    with pytest.raises(RevisionConflict, match="generation"):
        executor.execute(replace(REQUEST, expected_generation=1), forbidden)
    assert load(store).commands == ()


def test_recovery_preserves_explicit_terminal_decision_and_does_not_select_successor(store):
    started = QueueExecutor(store, Clock()).execute(REQUEST, receipt)
    finished = store.settle_attempt(
        "queue",
        "attempt",
        QueueIntent.FINISHED,
        decision_id="verified-ending",
        expected_revision=started.snapshot.revision,
    )
    executor = QueueExecutor(reopen(store), Clock())
    recovered = executor.recover(
        "queue", expected_revision=finished.revision, expected_generation=finished.generation
    )
    assert recovered == finished
    assert recovered.items[0].intent == QueueIntent.FINISHED
    assert recovered.items[1].intent == QueueIntent.PENDING
    assert len(recovered.commands) == 1
    assert not executor.execute(REQUEST, forbidden).operation_invoked


def test_recovered_pending_command_stays_fenced_even_after_reloading_new_generation(store):
    prepared = store.prepare_start(
        "queue",
        "first",
        attempt_id="attempt",
        command_id="start",
        at=NOW,
        expected_revision=0,
    )
    executor = QueueExecutor(reopen(store), Clock())
    recovered = executor.recover(
        "queue", expected_revision=prepared.revision, expected_generation=prepared.generation
    )
    request = replace(
        REQUEST, expected_revision=recovered.revision, expected_generation=recovered.generation
    )
    with pytest.raises(RevisionConflict, match="earlier runner generation"):
        executor.execute(request, forbidden)
    assert load(reopen(store)) == recovered
