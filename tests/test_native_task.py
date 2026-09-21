"""Native successors across the real task/application/SQLite boundaries, offline."""

import json
from contextlib import contextmanager
from dataclasses import replace
from threading import Event

import pytest

from tellyq.domain.native_queue import (
    NativeQueueAction,
    NativeQueueCapabilities,
    NativeQueueDiagnostic,
    NativeQueueReason,
    NativeQueueReceipt,
)
from tellyq.domain.queue import AttemptOrigin, ExecutionState, QueueEntry, QueueIntent, QueueMode
from tellyq.domain.values import (
    CapabilityEvidence,
    CommandAction,
    CommandOutcome,
    ContentPhase,
    ContentRef,
    ControlStage,
    IdleReason,
    PlaybackRequest,
    PlaybackTarget,
    PlayerState,
    Support,
)
from tellyq.native_task import NativePlaybackTask
from tellyq.queue_store import SQLiteQueueStore
from tellyq.runner import Cancellation, RunnerCommand
from tellyq.runner_codec import wire_view
from tellyq.service import load_manifest
from tests.playback_support import FakeClock, MemoryStore
from tests.test_playback_task import Backend

TARGET = PlaybackTarget("00000000-0000-4000-8000-000000000001", "cast")
ITEMS = tuple(
    QueueEntry(f"item-{i}", ContentRef("youtube", content))
    for i, content in enumerate(("AAAAAAAAAAA", "BBBBBBBBBBB", "AAAAAAAAAAA"))
)


class NativeBackend(Backend):
    def __init__(self, clock):
        super().__init__(clock)
        self.staged = None
        self.native_outcome = CommandOutcome.ACCEPTED
        self.clear_outcome = CommandOutcome.ACCEPTED
        self.native_supported = True
        self.before_native = None

    def event(self, **kwargs):
        if kwargs.get("application_id") == "app":
            kwargs["application_id"] = "233637DE"
        return replace(super().event(**kwargs), target=TARGET)

    def native_queue_capabilities(self, target):
        if not self.native_supported:
            return NativeQueueCapabilities()
        supported = CapabilityEvidence(Support.ADVERTISED, "synthetic", self.clock.utcnow())
        return NativeQueueCapabilities(supported, supported)

    def native_queue(self, request):
        if self.before_native:
            self.before_native(request)
        self.commands.append(
            (request.action.value, request.successor.content_id if request.successor else None)
        )
        outcome = self.native_outcome
        if request.action == NativeQueueAction.PLAY_NEXT:
            self.staged = request.successor
        else:
            outcome = self.clear_outcome
            if outcome == CommandOutcome.ACCEPTED:
                self.staged = None
        return NativeQueueReceipt(
            request,
            outcome,
            self.clock.utcnow(),
            NativeQueueDiagnostic(ControlStage.TRANSPORT, NativeQueueReason.COMMAND_RETURNED),
        )

    def transition(self, *, finish=True, ad=False, content=None):
        def batch():
            events = ()
            if finish:
                self.finish()
                events = super(NativeBackend, self).observe(TARGET)
            selected = content or self.staged
            assert selected is not None
            assert self.request is not None
            self.request = replace(self.request, content=selected)
            self.staged = None
            self.position = 0.0
            self.state = PlayerState.PLAYING
            self.phase = ContentPhase.AD if ad else ContentPhase.PLAYING
            self.ad = ad
            return events + super(NativeBackend, self).observe(TARGET)

        self.next_batch = batch


@pytest.fixture
def rig(tmp_path):
    clock, sessions, event = FakeClock(), MemoryStore(), Event()
    store = SQLiteQueueStore(tmp_path / "runtime")
    store.create("native", TARGET, ITEMS, mode=QueueMode.NATIVE)
    backend = NativeBackend(clock)

    @contextmanager
    def factory():
        yield backend

    task = NativePlaybackTask(factory, store, sessions, clock, queue_id="native", target=TARGET)
    yield task, backend, store, sessions, Cancellation(event), event, factory
    if not task._closed:
        task.close()


def start(rig):
    task, _, _, _, cancel, _, _ = rig
    request = PlaybackRequest("start", "attempt-a", ITEMS[0].item_id, ITEMS[0].content, TARGET)
    task.handle(RunnerCommand("start", CommandAction.START, request), cancel)
    return task.step(cancel)


def actions(backend):
    return [action for action, _ in backend.commands]


def test_three_items_have_one_start_two_native_handoffs_and_explicit_cleanup(rig):
    task, backend, store, _, cancel, event, _ = rig
    initial = start(rig)
    assert actions(backend) == ["start", "play_next"]
    assert initial.queue.items[1].intent == QueueIntent.PENDING
    assert len(initial.queue.attempts) == 1
    for index in (1, 2):
        backend.transition()
        view = task.step(cancel)
        assert view.queue.items[index - 1].intent == QueueIntent.FINISHED
        assert view.queue.items[index].intent == QueueIntent.CURRENT
        assert view.playback.evidence.receiver_playback_confirmed
        assert view.playback.receipt is None
        assert view.queue.attempts[-1].origin == AttemptOrigin.NATIVE_OBSERVED
    assert actions(backend) == ["start", "play_next", "play_next"]
    backend.finish()
    finished = task.step(cancel)
    assert all(item.intent == QueueIntent.FINISHED for item in finished.queue.items)
    event.set()
    stopped = task.handle(RunnerCommand("stop", CommandAction.STOP), cancel)
    assert stopped.playback.state == PlayerState.STOPPED
    assert all(item.intent == QueueIntent.FINISHED for item in stopped.queue.items)
    assert actions(backend) == ["start", "play_next", "play_next", "stop"]
    assert sum(c.action == CommandAction.START for c in store.load("native").commands) == 1


def test_ads_with_successor_identity_never_adopt_until_nonad_progress(rig):
    task, backend, _, _, cancel, _, _ = rig
    start(rig)
    backend.transition(ad=True)
    ads = task.step(cancel)
    assert ads.queue.items[0].intent == QueueIntent.FINISHED
    assert ads.queue.items[1].intent == QueueIntent.PENDING
    assert ads.handoff.reason.value == "native_successor_pending"
    assert actions(backend) == ["start", "play_next"]
    backend.ad, backend.phase = False, ContentPhase.PLAYING
    content = task.step(cancel)
    assert content.queue.items[1].intent == QueueIntent.CURRENT
    assert content.playback.evidence.receiver_playback_confirmed


def test_missing_predecessor_terminal_adopts_approved_b_but_holds_c(rig):
    task, backend, _, _, cancel, _, _ = rig
    start(rig)
    backend.transition(finish=False)
    view = task.step(cancel)
    assert view.queue.items[0].intent == QueueIntent.NEEDS_ATTENTION
    assert view.queue.items[1].intent == QueueIntent.CURRENT
    assert view.queue.reconciliation_required
    assert not view.queue.cancellation_requested
    task.step(cancel)
    assert actions(backend) == ["start", "play_next"]


@pytest.mark.parametrize("outcome", [CommandOutcome.UNKNOWN, CommandOutcome.REJECTED])
def test_enqueue_unknown_or_rejected_never_retries_or_falls_back(rig, outcome):
    task, backend, _, _, cancel, _, _ = rig
    backend.native_outcome = outcome
    start(rig)
    backend.finish()
    for _ in range(3):
        task.step(cancel)
    assert actions(backend) == ["start", "play_next"]


def test_takeover_callbacks_during_enqueue_cannot_be_erased_by_dispatch_boundary(rig):
    task, backend, _, _, cancel, _, _ = rig

    def during_enqueue(request):
        assert request.action == NativeQueueAction.PLAY_NEXT
        backend.transition(finish=False, content=ContentRef("youtube", "CCCCCCCCCCC"))
        takeover = backend.observe(TARGET)
        backend.transition(finish=False, content=ITEMS[1].content)
        successor = backend.observe(TARGET)
        backend.next_batch = lambda: takeover + successor

    backend.before_native = during_enqueue
    start(rig)
    view = task.step(cancel)
    assert view.queue.items[1].intent == QueueIntent.PENDING
    assert len(view.queue.attempts) == 1
    assert view.queue.reconciliation_required
    assert actions(backend) == ["start", "play_next"]


def test_unexpected_takeover_is_not_adopted_or_stopped(rig):
    task, backend, _, _, cancel, event, _ = rig
    start(rig)
    backend.transition(content=ContentRef("youtube", "CCCCCCCCCCC"))
    view = task.step(cancel)
    assert view.playback.ownership_lost
    assert view.queue.items[1].intent == QueueIntent.PENDING
    assert view.queue.reconciliation_required
    event.set()
    task.handle(RunnerCommand("stop", CommandAction.STOP), cancel)
    assert actions(backend) == ["start", "play_next"]


@pytest.mark.parametrize(
    "clear", [CommandOutcome.ACCEPTED, CommandOutcome.UNKNOWN, CommandOutcome.REJECTED]
)
def test_stop_commits_cancellation_then_clears_and_quits_even_when_clear_uncertain(rig, clear):
    task, backend, store, _, cancel, event, _ = rig
    start(rig)
    backend.clear_outcome = clear

    def inspect(request):
        assert request.action == NativeQueueAction.CLEAR
        assert store.load("native").cancellation_requested

    backend.before_native = inspect
    event.set()
    view = task.handle(RunnerCommand("stop", CommandAction.STOP), cancel)
    assert view.playback.state == PlayerState.STOPPED
    assert actions(backend) == ["start", "play_next", "clear", "stop"]
    assert (
        view.queue.native_operations[-1].state
        == {
            CommandOutcome.ACCEPTED: ExecutionState.ACKNOWLEDGED,
            CommandOutcome.UNKNOWN: ExecutionState.UNCERTAIN,
            CommandOutcome.REJECTED: ExecutionState.REJECTED,
        }[clear]
    )
    assert view.queue.reservations[0].state.value == "session_exit_observed"


def test_stopped_media_does_not_claim_receiver_session_exit(rig):
    task, backend, _, _, cancel, event, _ = rig
    start(rig)
    backend.stop_idle = False

    def stop_media(action):
        assert action == CommandAction.STOP
        backend.state = PlayerState.IDLE
        backend.next_batch = lambda: tuple(
            replace(e, idle_reason=IdleReason.CANCELED, provider_evidence=None)
            if e.playback_id
            else e
            for e in Backend.observe(backend, TARGET)
        )

    backend.after_effect = stop_media
    event.set()
    view = task.handle(RunnerCommand("stop-media", CommandAction.STOP), cancel)
    assert view.playback.state == PlayerState.STOPPED
    assert backend.active
    assert view.queue.reservations[0].state.value == "reserved"


@pytest.mark.parametrize("successor", [False, True])
def test_reopen_observes_authorized_a_or_b_without_any_device_command(rig, successor):
    task, backend, store, sessions, cancel, _, factory = rig
    start(rig)
    task.close()
    backend.generation = "reconnected"
    backend.sequence = 0
    if successor:
        backend.transition(finish=False)
    prior = tuple(backend.commands)
    reopened = NativePlaybackTask(
        factory, store, sessions, backend.clock, queue_id="native", target=TARGET
    )
    try:
        for _ in range(3):
            view = reopened.step(cancel)
        assert tuple(backend.commands) == prior
        assert view.queue is not None and view.playback is not None
        assert view.queue.reconciliation_required
        assert not view.queue.cancellation_requested
        assert view.playback.evidence.receiver_playback_confirmed
        assert view.playback.scope.connection_generation == "reconnected"
        assert view.playback.receipt is None
        assert view.queue.current_item_id == ITEMS[int(successor)].item_id
    finally:
        reopened.close()


def test_restart_after_b_adoption_reconciles_without_saved_b_snapshot(rig):
    task, backend, store, _sessions, cancel, _, factory = rig
    start(rig)
    backend.transition()
    view = task.step(cancel)
    task.close()
    empty = MemoryStore()
    backend.generation, backend.sequence = "new-owner", 0
    before = tuple(backend.commands)
    reopened = NativePlaybackTask(
        factory, store, empty, backend.clock, queue_id="native", target=TARGET
    )
    try:
        reconciled = reopened.step(cancel)
        assert reconciled.playback is not None
        assert (
            reconciled.playback.scope.request.attempt_id == view.playback.scope.request.attempt_id
        )
        assert reconciled.playback.evidence.receiver_playback_confirmed
        assert tuple(backend.commands) == before
    finally:
        reopened.close()


def test_status_projection_separates_native_commands_and_durable_hold(rig):
    view = json.loads(json.dumps(wire_view(start(rig))))
    queue = view["queue"]
    assert queue["mode"] == "native"
    assert not queue["reconciliation_required"]
    assert queue["native_reservations"][0]["state"] == "reserved"
    assert queue["native_operations"][0]["state"] == "acknowledged"
    assert queue["items"][1]["intent"] == "pending"


@pytest.mark.parametrize("resolution", ["playing", "timeout", "takeover", "reset"])
def test_stop_during_ad_handoff_waits_boundedly_for_authorized_successor(rig, resolution):
    task, backend, store, _, cancel, event, _ = rig
    start(rig)
    backend.transition(ad=True)
    event.set()
    waiting = task.handle(RunnerCommand("stop-during-ad", CommandAction.STOP), cancel)
    assert waiting.handoff.reason.value == "stop_awaiting_native_observation"
    assert actions(backend) == ["start", "play_next"]
    assert store.load("native").cancellation_requested
    if resolution == "timeout":
        backend.clock.tick(11)
    elif resolution == "takeover":
        backend.request = replace(backend.request, content=ContentRef("youtube", "CCCCCCCCCCC"))
    elif resolution == "reset":
        backend.generation = "new-generation"
    backend.ad, backend.phase = False, ContentPhase.PLAYING
    result = task.step(cancel)
    task.step(cancel)
    if resolution == "playing":
        assert result.playback.state == PlayerState.STOPPED
        assert actions(backend) == ["start", "play_next", "stop"]
    else:
        assert actions(backend) == ["start", "play_next"]
    assert task._pending_stop is None


@pytest.mark.parametrize("successor", [False, True])
def test_new_stop_after_reopen_can_wait_for_fresh_a_or_b_progress(rig, successor):
    task, backend, store, sessions, cancel, event, factory = rig
    start(rig)
    task.close()
    backend.generation, backend.sequence = "reopened", 0
    if successor:
        backend.transition(finish=False, ad=True)
    else:
        backend.ad, backend.phase = True, ContentPhase.AD
    reopened = NativePlaybackTask(
        factory, store, sessions, backend.clock, queue_id="native", target=TARGET
    )
    try:
        event.set()
        waiting = reopened.handle(RunnerCommand("new-stop", CommandAction.STOP), cancel)
        assert waiting.handoff is not None
        assert waiting.handoff.reason.value == "stop_awaiting_native_observation"
        backend.ad, backend.phase = False, ContentPhase.PLAYING
        stopped = reopened.step(cancel)
        assert stopped.playback is not None
        assert stopped.playback.state == PlayerState.STOPPED
        assert actions(backend) == ["start", "play_next"] + ([] if successor else ["clear"]) + [
            "stop"
        ]
        reopened.step(cancel)
        assert actions(backend).count("stop") == 1
    finally:
        reopened.close()


def test_native_manifest_is_opt_in_and_declines_adjacent_identical_content(tmp_path):
    path = tmp_path / "session.json"
    value = {
        "schema_version": 1,
        "queue_id": "native",
        "mode": "native",
        "target": {"device_id": TARGET.device_id, "route": "cast"},
        "items": [
            {"item_id": item.item_id, "provider": "youtube", "content_id": item.content.content_id}
            for item in ITEMS
        ],
    }
    path.write_text(json.dumps(value))
    assert load_manifest(path).mode == QueueMode.NATIVE
    value["items"][1]["content_id"] = value["items"][0]["content_id"]
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="adjacent"):
        load_manifest(path)
    del value["mode"]
    path.write_text(json.dumps(value))
    assert load_manifest(path).mode == QueueMode.LEGACY
