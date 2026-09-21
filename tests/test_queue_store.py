"""Crash boundaries and competing local writers; no device or network operations."""

import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from tellyq.domain.ports import RevisionConflict
from tellyq.domain.queue import ExecutionState, QueueEntry, QueueIntent, QueueStore
from tellyq.domain.values import ContentKind, ContentRef, PlaybackTarget
from tellyq.queue_store import QueueStoreError, SQLiteQueueStore
from tellyq.state import make_queue, save

NOW = datetime(2026, 9, 21, 15, tzinfo=UTC)
TARGET = PlaybackTarget("synthetic-device", "cast", "Test display")
ITEMS = (
    QueueEntry("first", ContentRef("youtube", "opaque-first", title="First")),
    QueueEntry("second", ContentRef("other-provider", "opaque:episode", ContentKind.EPISODE)),
    QueueEntry("third", ContentRef("youtube", "opaque-third")),
)


@pytest.fixture
def store(tmp_path):
    result = SQLiteQueueStore(tmp_path / "runtime")
    result.create("queue", TARGET, ITEMS)
    return result


def prepare(store, *, revision=0, attempt="attempt", command="start", item="first"):
    return store.prepare_start(
        "queue",
        item,
        attempt_id=attempt,
        command_id=command,
        at=NOW,
        expected_revision=revision,
    )


def dispatch(store, *, revision=1, command="start"):
    return store.mark_dispatched("queue", command, at=NOW, expected_revision=revision)


def acknowledge(store, *, revision=2, command="start"):
    return store.record_outcome(
        "queue",
        command,
        ExecutionState.ACKNOWLEDGED,
        at=NOW,
        expected_revision=revision,
    )


def test_atomic_selection_attempt_and_intent_are_durable(store):
    contract: QueueStore = store
    before = contract.load("queue")
    assert before is not None and before.items == ITEMS
    assert before.current_item_id is None
    selected = prepare(store)
    reopened = SQLiteQueueStore(store.path.parent).load("queue")
    assert reopened == selected
    assert selected.current_item_id == "first"
    assert selected.items[0].intent == QueueIntent.CURRENT
    assert selected.attempts[0].intent == QueueIntent.CURRENT
    assert selected.commands[0].state == ExecutionState.PENDING
    assert selected.revision == 1
    assert before.items[0].intent == QueueIntent.PENDING  # Detached immutable snapshot.


def test_command_receipt_does_not_change_queue_to_playing_or_finished(store):
    prepare(store)
    dispatch(store)
    after = acknowledge(store)
    assert after.items[0].intent == QueueIntent.CURRENT
    assert after.attempts[0].intent == QueueIntent.CURRENT
    assert after.commands[0].state == ExecutionState.ACKNOWLEDGED
    assert not hasattr(after, "observations")
    assert not hasattr(after, "monotonic")


def test_duplicate_command_identity_returns_existing_attempt_without_new_effect(store):
    selected = prepare(store)
    assert prepare(store) == selected  # Lost local response can be retried with old revision.
    with pytest.raises(QueueStoreError, match="another intent"):
        prepare(store, attempt="other")
    with pytest.raises(QueueStoreError, match="another item"):
        prepare(store, item="second")
    assert store.load("queue") == selected


def test_cas_blocks_competing_threads_across_independent_connections(store):
    barrier = Barrier(2)

    def writer(number):
        independent = SQLiteQueueStore(store.path.parent)
        barrier.wait(timeout=3)
        try:
            return prepare(independent, attempt=f"attempt-{number}", command=f"start-{number}")
        except RevisionConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(writer, range(2)))
    assert sum(result is not None for result in results) == 1
    snapshot = store.load("queue")
    assert snapshot is not None and len(snapshot.commands) == len(snapshot.attempts) == 1


def test_cas_blocks_another_process(store):
    script = """
import sys
from datetime import datetime, UTC
from pathlib import Path
from tellyq.queue_store import SQLiteQueueStore
store = SQLiteQueueStore(Path(sys.argv[1]))
store.prepare_start('queue', 'first', attempt_id='child-attempt', command_id='child-command',
                    at=datetime(2026, 9, 21, tzinfo=UTC), expected_revision=0)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(store.path.parent)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert completed.returncode == 0, completed.stderr
    with pytest.raises(RevisionConflict):
        prepare(store)
    snapshot = store.load("queue")
    assert snapshot is not None and snapshot.commands[0].command_id == "child-command"


def test_failed_intent_insert_rolls_back_queue_position_and_attempt(store):
    # An existing command belongs to another queue; collision occurs after item
    # selection and attempt insertion, exercising transaction rollback in practice.
    store.create("other", TARGET, ITEMS)
    store.prepare_start(
        "other",
        "first",
        attempt_id="other-attempt",
        command_id="collision",
        at=NOW,
        expected_revision=0,
    )
    before = store.load("queue")
    with pytest.raises(QueueStoreError):
        prepare(store, command="collision")
    assert store.load("queue") == before
    assert prepare(store).revision == 1


@pytest.mark.parametrize("boundary", ["pending", "dispatched", "acknowledged"])
def test_restart_requires_attention_without_resending_or_reusing_evidence(store, boundary):
    prepare(store)
    if boundary in {"dispatched", "acknowledged"}:
        dispatch(store)
    if boundary == "acknowledged":
        acknowledge(store)
    before = store.load("queue")
    assert before is not None
    reopened = SQLiteQueueStore(store.path.parent)
    assert reopened.load("queue") == before  # Opening does not interfere with a live owner.
    recovered = reopened.recover("queue", expected_revision=before.revision)
    assert recovered.items[0].intent == QueueIntent.NEEDS_ATTENTION
    assert recovered.items[1:].count(ITEMS[1]) == 1
    assert recovered.cancellation_requested
    assert recovered.generation == before.generation + 1
    expected = ExecutionState.UNCERTAIN if boundary == "dispatched" else ExecutionState(boundary)
    assert recovered.commands[0].state == expected
    assert recovered.attempts[0].decision_id is None
    assert reopened.recover("queue", expected_revision=recovered.revision) == recovered
    with pytest.raises(RevisionConflict):
        reopened.mark_dispatched("queue", "start", at=NOW, expected_revision=recovered.revision)
    with pytest.raises(QueueStoreError, match="reconciliation"):
        prepare(reopened, revision=recovered.revision, item="second", attempt="new", command="new")


def test_recovery_rejects_stale_worker_outcome_even_after_it_reloads_revision(store):
    prepare(store)
    dispatch(store)
    recovered = store.recover("queue", expected_revision=2)
    with pytest.raises(RevisionConflict, match="generation"):
        acknowledge(store, revision=recovered.revision)
    assert store.load("queue") == recovered


def test_completion_is_explicit_idempotent_and_does_not_start_or_select_next(store):
    prepare(store)
    dispatch(store)
    acknowledge(store)
    settled = store.settle_attempt(
        "queue", "attempt", QueueIntent.FINISHED, decision_id="observed-end-1", expected_revision=3
    )
    assert settled.items[0].intent == QueueIntent.FINISHED
    assert settled.items[1:] == ITEMS[1:]
    assert settled.current_item_id == "first"
    assert len(settled.commands) == 1
    reopened = SQLiteQueueStore(store.path.parent)
    assert reopened.recover("queue", expected_revision=4) == settled
    assert (
        reopened.settle_attempt(
            "queue",
            "attempt",
            QueueIntent.FINISHED,
            decision_id="observed-end-1",
            expected_revision=3,
        )
        == settled
    )
    with pytest.raises(QueueStoreError, match="terminal"):
        reopened.settle_attempt(
            "queue",
            "attempt",
            QueueIntent.SKIPPED,
            decision_id="other-decision",
            expected_revision=4,
        )
    selected = prepare(
        reopened, revision=4, item="second", attempt="second-attempt", command="second-start"
    )
    assert selected.items[0].intent == QueueIntent.FINISHED
    assert selected.items[1].content.provider == "other-provider"
    assert selected.items[1].content.kind == ContentKind.EPISODE


def test_duplicate_decision_for_different_attempt_rolls_back(store):
    prepare(store)
    dispatch(store)
    first = store.settle_attempt(
        "queue", "attempt", QueueIntent.FINISHED, decision_id="completion", expected_revision=2
    )
    second = prepare(
        store, revision=first.revision, item="second", attempt="next", command="next-start"
    )
    with pytest.raises(QueueStoreError):
        store.settle_attempt(
            "queue",
            "next",
            QueueIntent.FINISHED,
            decision_id="completion",
            expected_revision=second.revision,
        )
    assert store.load("queue") == second


def test_stop_cancels_undispatched_start_and_new_item_preparation(store):
    prepare(store)
    stopped = store.prepare_stop(
        "queue", attempt_id="attempt", command_id="stop", at=NOW, expected_revision=1
    )
    assert stopped.cancellation_requested
    assert (
        store.prepare_stop(
            "queue", attempt_id="attempt", command_id="stop", at=NOW, expected_revision=1
        )
        == stopped
    )
    with pytest.raises(QueueStoreError, match="canceled"):
        dispatch(store, revision=stopped.revision)
    with pytest.raises(QueueStoreError, match="natural completion"):
        store.settle_attempt(
            "queue",
            "attempt",
            QueueIntent.FINISHED,
            decision_id="incorrect-end",
            expected_revision=stopped.revision,
        )
    sent = dispatch(store, revision=stopped.revision, command="stop")
    accepted = acknowledge(store, revision=sent.revision, command="stop")
    final = store.settle_attempt(
        "queue",
        "attempt",
        QueueIntent.STOPPED,
        decision_id="observed-exit",
        expected_revision=accepted.revision,
    )
    assert final.items[0].intent == QueueIntent.STOPPED
    assert final.commands[0].state == ExecutionState.PENDING
    with pytest.raises(QueueStoreError, match="canceled"):
        prepare(store, revision=final.revision, item="second", attempt="next", command="next")


@pytest.mark.parametrize(
    "outcome", [QueueIntent.SKIPPED, QueueIntent.STOPPED, QueueIntent.NEEDS_ATTENTION]
)
def test_other_terminal_intents_do_not_claim_completion(store, outcome):
    prepare(store)
    result = store.settle_attempt(
        "queue", "attempt", outcome, decision_id="operator-decision", expected_revision=1
    )
    assert result.items[0].intent == outcome
    assert result.attempts[0].intent != QueueIntent.FINISHED
    assert result.items[1:] == ITEMS[1:]


def test_uncertain_command_cannot_be_redispatched(store):
    prepare(store)
    dispatch(store)
    uncertain = store.record_outcome(
        "queue", "start", ExecutionState.UNCERTAIN, at=NOW, expected_revision=2
    )
    with pytest.raises(QueueStoreError, match="redispatched"):
        dispatch(store, revision=uncertain.revision)
    assert (
        acknowledge(store, revision=uncertain.revision).commands[0].state
        == ExecutionState.ACKNOWLEDGED
    )


def test_invalid_transition_and_stale_timestamp_leave_state_intact(store):
    selected = prepare(store)
    with pytest.raises(QueueStoreError, match="dispatched"):
        acknowledge(store, revision=1)
    with pytest.raises(ValueError, match="backward"):
        store.mark_dispatched("queue", "start", at=NOW - timedelta(seconds=1), expected_revision=1)
    assert store.load("queue") == selected
    sent = dispatch(store)
    with pytest.raises(RevisionConflict):
        acknowledge(store, revision=1)
    assert store.load("queue") == sent


def test_future_schema_rejected_without_rewriting_file(store):
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute("PRAGMA user_version=900")
    before = store.path.read_bytes()
    with pytest.raises(QueueStoreError, match="unsupported"):
        SQLiteQueueStore(store.path.parent)
    assert store.path.read_bytes() == before


def test_unrecognized_schema_rejected_without_rewriting_file(store):
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute("ALTER TABLE queues ADD COLUMN unexpected TEXT")
    before = store.path.read_bytes()
    with pytest.raises(QueueStoreError, match="schema"):
        SQLiteQueueStore(store.path.parent)
    assert store.path.read_bytes() == before


def test_corrupt_database_is_not_replaced(tmp_path):
    path = tmp_path / "runtime"
    path.mkdir()
    database = path / "queue.sqlite3"
    database.write_bytes(b"not a SQLite database")
    with pytest.raises(QueueStoreError, match="invalid"):
        SQLiteQueueStore(path)
    assert database.read_bytes() == b"not a SQLite database"


def test_invalid_values_are_rejected_on_load(store):
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute("UPDATE items SET intent='invented'")
    with pytest.raises(QueueStoreError, match="durable"):
        store.load("queue")


def test_private_runtime_database_and_transaction_journal(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o755)
    store = SQLiteQueueStore(runtime)
    assert runtime.stat().st_mode & 0o777 == 0o700
    assert store.path.stat().st_mode & 0o777 == 0o600
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO queues VALUES ('q', 'd', 'r', NULL, 0, 0, NULL, 0)")
        assert store.path.with_name("queue.sqlite3-journal").stat().st_mode & 0o777 == 0o600
        connection.rollback()


def test_reject_symlink_database_and_runtime(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    runtime_link = tmp_path / "runtime-link"
    runtime_link.symlink_to(other, target_is_directory=True)
    with pytest.raises(QueueStoreError, match="symlink"):
        SQLiteQueueStore(runtime_link)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    original = other / "private"
    original.write_text("preserve")
    (runtime / "queue.sqlite3").symlink_to(original)
    with pytest.raises(QueueStoreError, match="regular"):
        SQLiteQueueStore(runtime)
    assert original.read_text() == "preserve"


@pytest.mark.parametrize(
    "state,intent",
    [
        ("queued", QueueIntent.PENDING),
        ("starting", QueueIntent.NEEDS_ATTENTION),
        ("playing", QueueIntent.NEEDS_ATTENTION),
        ("unconfirmed", QueueIntent.NEEDS_ATTENTION),
        ("failed", QueueIntent.NEEDS_ATTENTION),
        ("stopped", QueueIntent.STOPPED),
        ("finished", QueueIntent.FINISHED),
    ],
)
def test_explicit_legacy_import_preserves_source_and_historical_intent(tmp_path, state, intent):
    runtime = tmp_path / "runtime"
    queue = make_queue("00000000-0000-0000-0000-000000000001")
    queue["items"][0]["state"] = state
    save(runtime / "queue.json", queue)
    before = (runtime / "queue.json").read_bytes()
    store = SQLiteQueueStore(runtime)
    assert store.load("imported") is None  # Never migrate implicitly.
    imported = store.import_legacy(runtime, queue_id="imported")
    assert imported.items[0].intent == intent
    assert imported.cancellation_requested == (intent == QueueIntent.NEEDS_ATTENTION)
    assert imported.commands == imported.attempts == ()
    assert imported.current_item_id is None
    assert store.import_legacy(runtime, queue_id="imported") == imported
    assert (runtime / "queue.json").read_bytes() == before
    queue["updated_at"] = (NOW + timedelta(days=1)).isoformat()
    save(runtime / "queue.json", queue)
    with pytest.raises(QueueStoreError, match="changed"):
        store.import_legacy(runtime, queue_id="imported")
    assert store.load("imported") == imported


def test_malformed_legacy_import_cannot_create_partial_queue(tmp_path):
    runtime = tmp_path / "runtime"
    store = SQLiteQueueStore(runtime)
    (runtime / "queue.json").write_text('{"schema_version":2}')
    with pytest.raises(ValueError, match="version"):
        store.import_legacy(runtime, queue_id="imported")
    assert store.load("imported") is None


def test_new_queue_validation_and_existing_queue_protection(store):
    with pytest.raises(RevisionConflict):
        store.create("queue", TARGET, ITEMS)
    for entries in (
        (),
        (ITEMS[0], ITEMS[0]),
        (QueueEntry("bad", ITEMS[0].content, QueueIntent.FINISHED),),
    ):
        with pytest.raises(ValueError):
            store.create("invalid", TARGET, entries)
        assert store.load("invalid") is None
    with pytest.raises(ValueError, match="timezone"):
        store.prepare_start(
            "queue",
            "first",
            attempt_id="a",
            command_id="c",
            at=NOW.replace(tzinfo=None),
            expected_revision=0,
        )


def test_imports_are_inert_and_framework_free(tmp_path):
    project = Path(__file__).resolve().parents[1]
    script = """
import sys
from pathlib import Path
blocked = ('pychromecast', 'zeroconf', 'milo', 'chirp')
class BlockFrameworks:
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + '.') for name in blocked):
            raise AssertionError(fullname)
sys.meta_path.insert(0, BlockFrameworks())
import tellyq.domain.queue
import tellyq.queue_store
assert not Path('runtime').exists()
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(project)},
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("boundary", ["pending", "between-items"])
def test_local_cancellation_blocks_starts_without_needing_active_attempt(store, boundary):
    revision = 0
    if boundary == "between-items":
        prepare(store)
        dispatch(store)
        snapshot = store.settle_attempt(
            "queue",
            "attempt",
            QueueIntent.FINISHED,
            decision_id="observed-end",
            expected_revision=2,
        )
        revision = snapshot.revision
    canceled = store.cancel_queue("queue", expected_revision=revision)
    assert canceled.cancellation_requested
    assert all(command.action.value != "stop" for command in canceled.commands)
    assert store.cancel_queue("queue", expected_revision=canceled.revision) == canceled
    with pytest.raises(QueueStoreError, match="canceled"):
        prepare(store, revision=canceled.revision, item="second", attempt="next", command="next")


def test_start_and_cancel_race_blocks_dispatch_after_cancel_has_committed(store):
    barrier = Barrier(2)

    def start():
        barrier.wait(timeout=3)
        try:
            return prepare(store)
        except RevisionConflict:
            return None

    def cancel():
        barrier.wait(timeout=3)
        try:
            return store.cancel_queue("queue", expected_revision=0)
        except RevisionConflict:
            snapshot = store.load("queue")
            assert snapshot is not None
            return store.cancel_queue("queue", expected_revision=snapshot.revision)

    with ThreadPoolExecutor(max_workers=2) as executor:
        starts, cancels = executor.submit(start), executor.submit(cancel)
        started, canceled = starts.result(), cancels.result()
    assert canceled.cancellation_requested
    if started is not None:
        with pytest.raises(QueueStoreError, match="canceled"):
            dispatch(store, revision=canceled.revision)
    else:
        assert canceled.commands == ()


def test_settled_item_cannot_dispatch_old_pending_start(store):
    prepare(store)
    skipped = store.settle_attempt(
        "queue", "attempt", QueueIntent.SKIPPED, decision_id="operator-skip", expected_revision=1
    )
    with pytest.raises(QueueStoreError, match="current unresolved"):
        dispatch(store, revision=skipped.revision)
    selected = prepare(
        store, revision=skipped.revision, item="second", attempt="next", command="next"
    )
    with pytest.raises(QueueStoreError, match="current unresolved"):
        dispatch(store, revision=selected.revision)
    assert store.load("queue") == selected


def test_repeat_dispatch_is_not_an_authorization_to_resend(store):
    prepare(store)
    sent = dispatch(store)
    with pytest.raises(QueueStoreError, match="redispatched"):
        dispatch(store, revision=sent.revision)
    assert store.load("queue") == sent


def test_inconsistent_attempt_history_cannot_reopen_item_for_start(store):
    prepare(store)
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute("UPDATE items SET intent='pending' WHERE item_id='first'")
    with pytest.raises(QueueStoreError, match="durable"):
        store.load("queue")
    with pytest.raises(QueueStoreError, match="durable"):
        prepare(store, revision=1, item="second", attempt="other", command="other")


def test_orphaned_command_rejected_on_reopen(store):
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute(
            "INSERT INTO commands VALUES ('orphan', 'missing', 'start', 'pending', 0, ?, ?)",
            (NOW.isoformat(), NOW.isoformat()),
        )
    before = store.path.read_bytes()
    with pytest.raises(QueueStoreError, match="references"):
        SQLiteQueueStore(store.path.parent)
    assert store.path.read_bytes() == before
