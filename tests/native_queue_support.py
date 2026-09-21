"""Synthetic native queue authorizations and reducer-qualified observations."""

from datetime import UTC, datetime

from tellyq.domain.native_queue import (
    NativeQueueAction,
    NativeQueueDiagnostic,
    NativeQueueReason,
    NativeQueueReceipt,
    NativeQueueRequest,
)
from tellyq.domain.policy import observe
from tellyq.domain.queue import ExecutionState, QueueEntry, QueueMode
from tellyq.domain.values import (
    CommandOutcome,
    ContentRef,
    ControlStage,
    IdentityUpdate,
    PlaybackObservation,
    PlaybackRequest,
    PlaybackScope,
    PlaybackTarget,
    PlayerState,
    SessionSnapshot,
)

NOW = datetime(2026, 9, 21, 16, tzinfo=UTC)
TARGET = PlaybackTarget("synthetic-target", "cast")
ITEMS = tuple(
    QueueEntry(name, ContentRef("youtube", content))
    for name, content in (
        ("a", "video-a"),
        ("b", "video-b"),
        ("c", "video-a"),
    )
)


class Clock:
    def utcnow(self):
        return NOW

    def monotonic(self):
        raise AssertionError("durable execution must not recover monotonic clocks")


def scope(item="a", *, connection="connection", start=10):
    content = next(entry.content for entry in ITEMS if entry.item_id == item)
    return PlaybackScope(
        PlaybackRequest("observe-" + item, "attempt-" + item, item, content, TARGET),
        "session",
        connection,
        start,
        "application",
    )


def request(item="a", successor="b", operation_id="enqueue"):
    return NativeQueueRequest(
        operation_id,
        NativeQueueAction.PLAY_NEXT,
        scope(item),
        "media-" + item,
        next(entry.content for entry in ITEMS if entry.item_id == successor),
    )


def clear(item="a", operation_id="clear"):
    return NativeQueueRequest(operation_id, NativeQueueAction.CLEAR, scope(item), "media-" + item)


def load(store):
    snapshot = store.load("queue")
    assert snapshot is not None
    return snapshot


def started(store, *, mode=QueueMode.NATIVE, items=ITEMS):
    store.create("queue", TARGET, items, mode=mode)
    store.prepare_start(
        "queue", "a", attempt_id="attempt-a", command_id="start-a", at=NOW, expected_revision=0
    )
    store.mark_dispatched("queue", "start-a", at=NOW, expected_revision=1)
    return store.record_outcome(
        "queue", "start-a", ExecutionState.ACKNOWLEDGED, at=NOW, expected_revision=2
    )


def reserve(
    store, *, native_request=None, reservation_id="reservation", successor="b", revision=None
):
    before = load(store)
    return store.prepare_native_successor(
        "queue",
        successor,
        reservation_id=reservation_id,
        successor_attempt_id="attempt-" + successor,
        request=native_request or request(),
        at=NOW,
        expected_revision=before.revision if revision is None else revision,
        expected_generation=before.generation,
    )


def staged(store, *, outcome=ExecutionState.ACKNOWLEDGED):
    prepared = reserve(store)
    claimed = store.mark_native_dispatched(
        "queue",
        "enqueue",
        at=NOW,
        expected_revision=prepared.revision,
        expected_generation=prepared.generation,
    )
    return store.record_native_outcome(
        "queue",
        "enqueue",
        outcome,
        at=NOW,
        expected_revision=claimed.revision,
        expected_generation=claimed.generation,
    )


def successor_observed(item="b", *, connection="connection", start=10):
    owned = scope(item, connection=connection, start=start)
    snapshot = SessionSnapshot(owned)
    for sequence in (1, 2):
        observation = PlaybackObservation(
            TARGET,
            connection,
            sequence,
            NOW,
            start + sequence,
            session_id=owned.session_id,
            content=owned.request.content,
            state=PlayerState.PLAYING,
            position=float(sequence),
            ad_active=False,
            application_id=owned.application_id,
            playback_id="media-" + item,
            identity_update=IdentityUpdate.EXPLICIT,
        )
        snapshot = observe(snapshot, observation, now=observation.monotonic)
    assert snapshot.evidence.receiver_playback_confirmed
    return snapshot


def adopt(store, *, observed=None, reservation_id="reservation", decision_id="adopt-b", now=12):
    before = load(store)
    return store.adopt_native_successor(
        "queue",
        reservation_id,
        observed=observed or successor_observed(),
        now=now,
        at=NOW,
        decision_id=decision_id,
        expected_revision=before.revision,
        expected_generation=before.generation,
    )


def receipt(dispatch, outcome=CommandOutcome.ACCEPTED):
    return NativeQueueReceipt(
        dispatch.request,
        outcome,
        NOW,
        NativeQueueDiagnostic(ControlStage.TRANSPORT, NativeQueueReason.COMMAND_RETURNED),
    )
