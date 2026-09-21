"""Durable native intent, migration and passive adoption; no receiver effects."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from threading import Barrier

import pytest

from tellyq.domain.ports import RevisionConflict
from tellyq.domain.queue import (
    AttemptOrigin,
    ExecutionState,
    NativeReservationState,
    QueueEntry,
    QueueIntent,
    QueueMode,
)
from tellyq.domain.values import CommandAction, IdentityUpdate, PlayerState
from tellyq.queue_store import _SCHEMA_V1, QueueStoreError, SQLiteQueueStore

from .native_queue_support import (
    ITEMS,
    NOW,
    adopt,
    clear,
    load,
    request,
    reserve,
    staged,
    started,
    successor_observed,
)


@pytest.fixture
def store(tmp_path):
    result = SQLiteQueueStore(tmp_path / "runtime")
    started(result)
    return result


def finish(store, attempt="attempt-a", decision="finish-a"):
    return store.settle_attempt(
        "queue",
        attempt,
        QueueIntent.FINISHED,
        decision_id=decision,
        expected_revision=load(store).revision,
    )


def test_migration_preserves_all_v1_rows_and_legacy_mode(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    path = runtime / "queue.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        for statement in _SCHEMA_V1:
            connection.execute(statement)
        connection.execute("PRAGMA user_version=1")
        connection.execute(
            "INSERT INTO queues VALUES ('old','target','cast',NULL,7,2,'old-item',0)"
        )
        connection.execute(
            "INSERT INTO items VALUES ('old','old-item',0,'youtube','old-video','video',NULL,'finished')"
        )
        connection.execute(
            "INSERT INTO attempts VALUES ('old-attempt','old','old-item','finished',?,'decision')",
            (NOW.isoformat(),),
        )
        connection.execute(
            "INSERT INTO commands VALUES ('old-command','old-attempt','start','acknowledged',2,?,?)",
            (NOW.isoformat(), NOW.isoformat()),
        )
        connection.execute("INSERT INTO imports VALUES ('old','digest')")
        connection.commit()
        before = {
            table: connection.execute("SELECT * FROM " + table).fetchall()
            for table in ("queues", "items", "attempts", "commands", "imports")
        }
    migrated = SQLiteQueueStore(runtime)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert before == {
            table: connection.execute("SELECT * FROM " + table).fetchall() for table in before
        }
    snapshot = migrated.load("old")
    assert snapshot is not None
    assert snapshot.mode == QueueMode.LEGACY
    assert snapshot.revision == 7 and snapshot.generation == 2
    assert not snapshot.reconciliation_required
    assert snapshot.reservations == snapshot.native_operations == ()
    assert snapshot.attempts[0].origin == AttemptOrigin.LOCAL_START


@pytest.mark.parametrize("corrupt", ("schema", "value"))
def test_invalid_v1_is_retained_without_partial_migration(tmp_path, corrupt):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    path = runtime / "queue.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        for statement in _SCHEMA_V1:
            connection.execute(statement)
        connection.execute("PRAGMA user_version=1")
        if corrupt == "schema":
            connection.execute("CREATE TABLE unrecognized (value TEXT)")
        else:
            connection.execute("INSERT INTO queues VALUES ('old','target','cast',NULL,0,0,NULL,0)")
            connection.execute(
                "INSERT INTO items VALUES ('old','item',0,'youtube','video','invalid',NULL,'pending')"
            )
        connection.commit()
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    with pytest.raises(QueueStoreError):
        SQLiteQueueStore(runtime)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            == tables
        )


def test_native_mode_is_opt_in_and_reservation_does_not_select_successor(tmp_path, store):
    legacy = SQLiteQueueStore(tmp_path / "legacy")
    started(legacy, mode=QueueMode.LEGACY)
    with pytest.raises(QueueStoreError, match="native queue mode"):
        reserve(legacy)
    after = reserve(store)
    assert after.mode == QueueMode.NATIVE
    assert after.current_item_id == "a" and after.items[1].intent == QueueIntent.PENDING
    assert len(after.attempts) == 1
    assert after.reservations[0].successor_attempt_id == "attempt-b"
    assert after.native_operations[0].state == ExecutionState.PENDING
    assert SQLiteQueueStore(store.path.parent).load("queue") == after


def test_one_outstanding_reservation_and_no_local_start_fallback(store):
    first = staged(store)
    with pytest.raises(QueueStoreError, match="already reserved"):
        reserve(store, native_request=request(operation_id="other"), reservation_id="other")
    finish(store)
    with pytest.raises(QueueStoreError, match="fallback"):
        store.prepare_release(
            "queue",
            attempt_id="attempt-a",
            command_id="release",
            at=NOW,
            expected_revision=load(store).revision,
        )
    with pytest.raises(QueueStoreError, match="never started"):
        store.prepare_start(
            "queue",
            "b",
            attempt_id="attempt-b",
            command_id="start-b",
            at=NOW,
            expected_revision=load(store).revision,
        )
    assert len(load(store).native_operations) == len(first.native_operations)


def test_adjacent_repeated_content_requires_occurrence_evidence(tmp_path):
    store = SQLiteQueueStore(tmp_path / "runtime")
    started(store, items=(ITEMS[0], QueueEntry("b", ITEMS[0].content)))
    with pytest.raises(QueueStoreError, match="repeated content"):
        reserve(store, native_request=replace(request(), successor=ITEMS[0].content))
    assert not load(store).reservations


def test_competing_reservations_claim_one_slot(store):
    barrier = Barrier(2)
    revision = load(store).revision

    def write(number):
        other = SQLiteQueueStore(store.path.parent)
        barrier.wait(timeout=3)
        try:
            return reserve(
                other,
                native_request=request(operation_id=f"op-{number}"),
                reservation_id=f"r-{number}",
                revision=revision,
            )
        except RevisionConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, range(2)))
    assert sum(result is not None for result in results) == 1
    assert len(load(store).reservations) == 1


@pytest.mark.parametrize("completed", (False, True))
def test_adoption_is_atomic_without_start_or_fabricated_predecessor_completion(store, completed):
    staged(store)
    if completed:
        finish(store)
    after = adopt(store)
    assert after.current_item_id == "b"
    assert after.attempts[0].intent == (
        QueueIntent.FINISHED if completed else QueueIntent.NEEDS_ATTENTION
    )
    assert after.reconciliation_required is not completed
    assert not after.cancellation_requested
    assert after.attempts[1].intent == QueueIntent.CURRENT
    assert after.attempts[1].origin == AttemptOrigin.NATIVE_OBSERVED
    assert after.attempts[1].reservation_id == "reservation"
    assert len(after.commands) == 1 and after.commands[0].action == CommandAction.START
    assert after.reservations[0].state == NativeReservationState.ADOPTED
    assert SQLiteQueueStore(store.path.parent).load("queue") == after
    assert adopt(store) == after
    with pytest.raises(QueueStoreError, match="adoption decision"):
        adopt(store, decision_id="different")


def test_nonadjacent_repeat_is_a_distinct_reserved_occurrence(store):
    staged(store)
    finish(store)
    adopt(store)
    after = reserve(
        store,
        native_request=request("b", "c", "enqueue-c"),
        successor="c",
        reservation_id="reservation-c",
    )
    assert len(after.reservations) == 2
    assert sum(r.state == NativeReservationState.RESERVED for r in after.reservations) == 1
    assert after.reservations[-1].successor_attempt_id == "attempt-c"
    assert after.items[2].content == after.items[0].content


@pytest.mark.parametrize("state", (ExecutionState.PENDING, ExecutionState.REJECTED))
def test_uninvoked_or_rejected_reservation_cannot_be_adopted(store, state):
    reserve(store) if state == ExecutionState.PENDING else staged(store, outcome=state)
    with pytest.raises(QueueStoreError, match="qualified successor progress"):
        adopt(store)
    assert len(load(store).attempts) == 1


@pytest.mark.parametrize(
    "change",
    (
        {"ownership_lost": True},
        {"stop_requested": True},
        {"has_confirmed_playback": False},
    ),
)
def test_adoption_rejects_unowned_or_unconfirmed_snapshot(store, change):
    staged(store)
    with pytest.raises(QueueStoreError, match="qualified successor progress"):
        adopt(store, observed=replace(successor_observed(), **change))


@pytest.mark.parametrize(
    "change",
    (
        {"ad_active": True},
        {"ad_active": None},
        {"state": PlayerState.BUFFERING},
        {"identity_update": IdentityUpdate.OMITTED},
        {"session_id": "other"},
        {"application_id": "other"},
        {"connection_generation": "other"},
        {"content": ITEMS[0].content},
        {"playback_id": None},
    ),
)
def test_adoption_rejects_ads_unknown_identity_or_changed_scope(store, change):
    staged(store)
    observed = successor_observed()
    assert observed.latest is not None
    with pytest.raises(QueueStoreError, match="qualified successor progress"):
        adopt(store, observed=replace(observed, latest=replace(observed.latest, **change)))


@pytest.mark.parametrize("now", (11, 17.001))
def test_adoption_rejects_stale_or_future_evidence(store, now):
    staged(store)
    with pytest.raises(QueueStoreError, match="qualified successor progress"):
        adopt(store, now=now)


@pytest.mark.parametrize("finished", (False, True))
def test_recovery_fences_staged_work_even_after_a_finished(store, finished):
    prepared = reserve(store)
    before = store.mark_native_dispatched(
        "queue", "enqueue", at=NOW, expected_revision=prepared.revision, expected_generation=0
    )
    if finished:
        before = finish(store)
    recovered = store.recover("queue", expected_revision=before.revision)
    assert recovered.generation == 1
    assert recovered.reconciliation_required and not recovered.cancellation_requested
    assert recovered.attempts[0].intent == (
        QueueIntent.FINISHED if finished else QueueIntent.CURRENT
    )
    assert recovered.native_operations[0].state == ExecutionState.UNCERTAIN
    with pytest.raises(RevisionConflict):
        store.record_native_outcome(
            "queue",
            "enqueue",
            ExecutionState.ACKNOWLEDGED,
            at=NOW,
            expected_revision=recovered.revision,
            expected_generation=0,
        )
    with pytest.raises(RevisionConflict):
        store.mark_native_dispatched(
            "queue", "enqueue", at=NOW, expected_revision=recovered.revision, expected_generation=1
        )
    adopted = adopt(
        store, observed=successor_observed(connection="reconnected", start=100), now=102
    )
    assert adopted.current_item_id == "b" and adopted.reconciliation_required
    assert adopted.generation == 1


def test_hold_and_recovery_never_cancel_and_stop_still_targets_adopted_current(store):
    staged(store)
    after = adopt(store)
    assert after.items[0].intent == QueueIntent.NEEDS_ATTENTION
    held = store.hold_for_reconciliation("queue", expected_revision=after.revision)
    assert held == after and not held.cancellation_requested
    recovered = store.recover("queue", expected_revision=held.revision)
    assert recovered.attempts[-1].intent == QueueIntent.CURRENT
    stopped = store.prepare_stop(
        "queue",
        attempt_id="attempt-b",
        command_id="stop-b",
        at=NOW,
        expected_revision=recovered.revision,
    )
    claimed = store.mark_dispatched("queue", "stop-b", at=NOW, expected_revision=stopped.revision)
    assert claimed.cancellation_requested
    assert claimed.commands[-1].state == ExecutionState.DISPATCHED
    assert claimed.commands[-1].attempt_id == "attempt-b"


def test_successor_starting_after_cancel_can_be_adopted_for_owned_stop(store):
    queued = staged(store)
    store.cancel_queue("queue", expected_revision=queued.revision)
    observed = adopt(store)
    assert observed.cancellation_requested and observed.reconciliation_required
    assert observed.current_item_id == "b"
    stopped = store.prepare_stop(
        "queue",
        attempt_id="attempt-b",
        command_id="stop-b",
        at=NOW,
        expected_revision=observed.revision,
    )
    assert stopped.commands[-1].attempt_id == "attempt-b"
    assert stopped.items[0].intent == QueueIntent.NEEDS_ATTENTION


def test_native_current_controls_remain_available_with_predecessor_attention_hold(store):
    staged(store)
    observed = adopt(store)
    paused = store.prepare_control(
        "queue",
        attempt_id="attempt-b",
        command_id="pause-b",
        action=CommandAction.PAUSE,
        at=NOW,
        expected_revision=observed.revision,
    )
    dispatched = store.mark_dispatched(
        "queue", "pause-b", at=NOW, expected_revision=paused.revision
    )
    assert dispatched.commands[-1].attempt_id == "attempt-b"
    assert dispatched.reconciliation_required and not dispatched.cancellation_requested


@pytest.mark.parametrize("adopted", (False, True))
def test_clear_requires_cancellation_accepts_current_a_or_b_and_unknown_allows_stop(store, adopted):
    staged(store)
    if adopted:
        adopt(store)
    native_clear = clear("b" if adopted else "a")
    before = load(store)
    with pytest.raises(QueueStoreError, match="cancellation"):
        store.prepare_native_clear(
            "queue",
            "reservation",
            request=native_clear,
            at=NOW,
            expected_revision=before.revision,
            expected_generation=0,
        )
    canceled = store.cancel_queue("queue", expected_revision=before.revision)
    prepared = store.prepare_native_clear(
        "queue",
        "reservation",
        request=native_clear,
        at=NOW,
        expected_revision=canceled.revision,
        expected_generation=0,
    )
    dispatched = store.mark_native_dispatched(
        "queue", "clear", at=NOW, expected_revision=prepared.revision, expected_generation=0
    )
    unknown = store.record_native_outcome(
        "queue",
        "clear",
        ExecutionState.UNCERTAIN,
        at=NOW,
        expected_revision=dispatched.revision,
        expected_generation=0,
    )
    assert unknown.reservations[0].exit_decision_id is None
    stopped = store.prepare_stop(
        "queue",
        attempt_id="attempt-b" if adopted else "attempt-a",
        command_id="stop",
        at=NOW,
        expected_revision=unknown.revision,
    )
    assert stopped.commands[-1].action == CommandAction.STOP
    with pytest.raises(QueueStoreError):
        store.prepare_native_clear(
            "queue",
            "reservation",
            request=replace(native_clear, operation_id="retry"),
            at=NOW,
            expected_revision=stopped.revision,
            expected_generation=0,
        )
    exited = store.record_native_session_exit(
        "queue",
        "reservation",
        at=NOW,
        decision_id="fresh-idle",
        expected_revision=stopped.revision,
        expected_generation=0,
    )
    assert exited.reservations[0].state == NativeReservationState.SESSION_EXIT_OBSERVED
    assert exited.reconciliation_required and exited.cancellation_requested
