"""Real application/store composition with a bounded, synthetic receiver."""

from contextlib import contextmanager
from dataclasses import replace
from threading import Event, get_ident

import pytest

from tellyq.domain import policy
from tellyq.domain.queue import ExecutionState, QueueEntry, QueueIntent
from tellyq.domain.values import (
    CapabilityEvidence,
    CommandAction,
    CommandOutcome,
    CommandReceipt,
    ContentPhase,
    ContentRef,
    IdentityUpdate,
    IdleReason,
    PlaybackCapabilities,
    PlaybackObservation,
    PlaybackRequest,
    PlaybackTarget,
    PlayerState,
    ProviderEvidence,
    Support,
)
from tellyq.playback_task import PlaybackTask
from tellyq.queue_execution import OutcomePersistenceError, QueueCommandRequest
from tellyq.queue_store import QueueStoreError, SQLiteQueueStore
from tellyq.runner import Cancellation, RunnerCommand
from tellyq.session_store import decode_snapshot, encode_snapshot
from tests.playback_support import FakeClock, MemoryStore

TARGET = PlaybackTarget("synthetic-device", "fake")
ITEMS = tuple(
    QueueEntry(f"item-{index}", ContentRef("fake", f"content-{index}")) for index in range(3)
)


class Backend:
    def __init__(self, clock):
        self.clock = clock
        self.sequence = 0
        self.generation = "connection"
        self.request = None
        self.state = PlayerState.PLAYING
        self.phase = ContentPhase.PLAYING
        self.position = 0.0
        self.ad = False
        self.active = False
        self.commands = []
        self.calls = []
        self.before_observe = None
        self.after_effect = None
        self.stop_outcome = CommandOutcome.ACCEPTED
        self.stop_idle = True
        self.replace_on_terminal = False
        self.omit_identity = False
        self.reset = False
        self.next_batch = None

    def capabilities(self, target):
        support = CapabilityEvidence(Support.VERIFIED, "fake", self.clock.utcnow())
        return PlaybackCapabilities(pause=support, resume=support)

    def receipt(self, request, action, outcome=CommandOutcome.ACCEPTED):
        return CommandReceipt(
            request.request_id, request.attempt_id, action, outcome, self.clock.utcnow()
        )

    def start(self, request):
        self.calls.append(get_ident())
        self.commands.append(("start", request.content.content_id))
        self.request, self.active = request, True
        self.state, self.phase, self.position = PlayerState.PLAYING, ContentPhase.PLAYING, 0.0
        if self.after_effect:
            self.after_effect(CommandAction.START)
        return self.receipt(request, CommandAction.START)

    def stop(self, scope):
        self.calls.append(get_ident())
        self.commands.append(("stop", scope.request.content.content_id))
        if self.stop_idle:
            self.active = False
        if self.after_effect:
            self.after_effect(CommandAction.STOP)
        return self.receipt(scope.request, CommandAction.STOP, self.stop_outcome)

    def pause(self, scope, playback_id):
        self.commands.append(("pause", scope.request.content.content_id))
        self.state, self.phase = PlayerState.PAUSED, ContentPhase.PAUSED
        return self.receipt(scope.request, CommandAction.PAUSE)

    def resume(self, scope, playback_id):
        self.commands.append(("resume", scope.request.content.content_id))
        self.state, self.phase = PlayerState.PLAYING, ContentPhase.PLAYING
        return self.receipt(scope.request, CommandAction.RESUME)

    def event(self, **kwargs):
        self.sequence += 1
        return PlaybackObservation(
            TARGET,
            self.generation,
            self.sequence,
            self.clock.utcnow(),
            self.clock.tick(0.1),
            **kwargs,
        )

    def observe(self, target):
        self.calls.append(get_ident())
        if self.next_batch is not None:
            call, self.next_batch = self.next_batch, None
            return call()
        if self.before_observe:
            self.before_observe()
        if not self.active:
            return (self.event(session_active=False),)
        events = [
            self.event(
                session_id="session",
                application_id="app",
                session_active=True,
                connection_reset=self.reset,
            )
        ]
        assert self.request is not None
        for _ in range(2 if self.state == PlayerState.PLAYING else 1):
            self.clock.tick(1.0 if self.state == PlayerState.PLAYING else 0.01)
            if self.state == PlayerState.PLAYING:
                self.position += 1.2
            events.append(
                self.event(
                    session_id="session",
                    application_id="app",
                    playback_id="media",
                    content=None if self.omit_identity else self.request.content,
                    identity_update=IdentityUpdate.OMITTED
                    if self.omit_identity
                    else IdentityUpdate.EXPLICIT,
                    state=self.state,
                    position=self.position,
                    duration=5.0,
                    ad_active=self.ad,
                    idle_reason=IdleReason.FINISHED if self.state == PlayerState.IDLE else None,
                    provider_evidence=ProviderEvidence("fake-contract", self.phase),
                )
            )
        if self.replace_on_terminal and self.state == PlayerState.IDLE:
            events.append(
                replace(
                    events[-1],
                    sequence=self.sequence + 1,
                    monotonic=self.clock.tick(0.1),
                    content=ContentRef("fake", "autoplay"),
                )
            )
            self.sequence += 1
        return tuple(events)

    def finish(self):
        self.state, self.phase = PlayerState.IDLE, ContentPhase.FINISHED
        self.position = 5.0


@pytest.fixture
def rig(tmp_path):
    clock, store, sessions = FakeClock(), SQLiteQueueStore(tmp_path / "runtime"), MemoryStore()
    store.create("queue", TARGET, ITEMS)
    backend, event, lifecycle = Backend(clock), Event(), []

    @contextmanager
    def factory():
        lifecycle.append(("enter", get_ident()))
        try:
            yield backend
        finally:
            lifecycle.append(("exit", get_ident()))

    task = PlaybackTask(factory, store, sessions, clock, queue_id="queue", target=TARGET)
    yield task, backend, store, Cancellation(event), event, lifecycle
    if not task._closed:
        task.close()


def start(task, cancellation):
    request = PlaybackRequest("start", "attempt", ITEMS[0].item_id, ITEMS[0].content, TARGET)
    return task.handle(RunnerCommand("start", CommandAction.START, request), cancellation)


def test_three_items_require_finished_release_observed_idle_then_successor(rig):
    task, backend, store, cancellation, _, lifecycle = rig
    assert backend.commands == []
    assert task.step(cancellation).queue.items[0].intent == QueueIntent.PENDING
    result = start(task, cancellation)
    assert result.playback.evidence.receiver_playback_confirmed
    for index in range(3):
        backend.finish()
        settled = task.step(cancellation)
        assert settled.queue.items[index].intent == QueueIntent.FINISHED
        assert settled.playback.completion is not None
        assert len([action for action, _ in backend.commands if action == "start"]) == index + 1
        released = task.step(cancellation)
        assert released.playback.release_confirmed
        assert not released.playback.stop_requested
        assert released.playback.completion == settled.playback.completion
        assert not released.queue.cancellation_requested
        assert released.queue.commands[-1].action == CommandAction.RELEASE
        assert released.queue.commands[-1].state == ExecutionState.ACKNOWLEDGED
        task.step(cancellation)
    assert backend.commands == [
        (action, item.content.content_id) for item in ITEMS for action in ("start", "stop")
    ]
    assert all(item.intent == QueueIntent.FINISHED for item in store.load("queue").items)
    assert set(backend.calls) == {get_ident()}
    task.close()
    assert lifecycle == [("enter", get_ident()), ("exit", get_ident())]


def test_duplicate_start_never_relaunches_and_external_release_is_rejected(rig):
    task, backend, _, cancellation, _, _ = rig
    start(task, cancellation)
    start(task, cancellation)
    assert backend.commands == [("start", ITEMS[0].content.content_id)]
    with pytest.raises(ValueError, match="internal"):
        task.handle(RunnerCommand("release", CommandAction.RELEASE), cancellation)


def test_pause_resume_are_journaled_and_do_not_advance(rig):
    task, backend, store, cancellation, _, _ = rig
    start(task, cancellation)
    paused = task.handle(RunnerCommand("pause", CommandAction.PAUSE), cancellation)
    assert paused.playback.state == PlayerState.PAUSED
    task.step(cancellation)
    resumed = task.handle(RunnerCommand("resume", CommandAction.RESUME), cancellation)
    assert resumed.playback.state == PlayerState.PLAYING
    assert resumed.queue.items[0].intent == QueueIntent.CURRENT
    task.handle(RunnerCommand("resume", CommandAction.RESUME), cancellation)
    assert [a for a, _ in backend.commands] == ["start", "pause", "resume"]
    assert [c.action for c in store.load("queue").commands] == [
        CommandAction.START,
        CommandAction.PAUSE,
        CommandAction.RESUME,
    ]


def test_operator_stop_cancels_even_when_latch_is_set(rig):
    task, backend, _, cancellation, event, _ = rig
    start(task, cancellation)
    event.set()
    stopped = task.handle(RunnerCommand("stop", CommandAction.STOP), cancellation)
    assert stopped.playback.state == PlayerState.STOPPED
    assert stopped.queue.cancellation_requested
    assert stopped.queue.items[0].intent == QueueIntent.STOPPED
    assert [a for a, _ in backend.commands] == ["start", "stop"]


def test_incremental_stop_crosses_owner_wrapper_and_persists_observed_outcome(rig, monkeypatch):
    task, backend, store, cancellation, event, _ = rig
    start(task, cancellation)
    observe = backend.observe
    batches = []

    def observe_until(target, ready):
        events = observe(target)
        batches.append(events)
        assert ready(events)
        return events

    def fixed_window(_target):
        raise AssertionError("Stop should use the optional prompt observation capability")

    monkeypatch.setattr(backend, "observe_until", observe_until, raising=False)
    monkeypatch.setattr(backend, "observe", fixed_window)
    event.set()
    stopped = task.handle(RunnerCommand("stop", CommandAction.STOP), cancellation)
    assert len(batches) == 2
    assert task._events == batches[-1]
    assert stopped.playback.state == PlayerState.STOPPED
    assert stopped.queue.items[0].intent == QueueIntent.STOPPED
    assert store.load("queue").commands[-1].state == ExecutionState.ACKNOWLEDGED
    assert [a for a, _ in backend.commands] == ["start", "stop"]


def test_terminal_and_replacement_in_one_batch_settles_but_never_releases(rig):
    task, backend, _, cancellation, _, _ = rig
    start(task, cancellation)
    backend.finish()
    backend.replace_on_terminal = True
    result = task.step(cancellation)
    assert result.queue.items[0].intent == QueueIntent.FINISHED
    assert result.playback.ownership_lost
    task.step(cancellation)
    assert [a for a, _ in backend.commands] == ["start"]


@pytest.mark.parametrize("hazard", ["code5-reset", "ad", "replay", "connection", "omitted"])
def test_post_terminal_hazards_hold_without_release_or_successor(rig, hazard):
    task, backend, _, cancellation, _, _ = rig
    start(task, cancellation)
    backend.finish()
    task.step(cancellation)
    if hazard == "code5-reset":
        backend.state, backend.phase, backend.ad, backend.position = (
            PlayerState.BUFFERING,
            ContentPhase.UNKNOWN,
            None,
            0.0,
        )
    elif hazard == "ad":
        backend.state, backend.phase, backend.ad = PlayerState.BUFFERING, ContentPhase.AD, True
    elif hazard == "replay":
        backend.state, backend.phase = PlayerState.PLAYING, ContentPhase.PLAYING
    elif hazard == "connection":
        backend.generation, backend.reset = "new-connection", True
    else:
        backend.omit_identity = True
    result = task.step(cancellation)
    assert result.queue.items[0].intent == QueueIntent.FINISHED
    assert result.playback.completion is not None
    assert not result.playback.release_confirmed
    assert [a for a, _ in backend.commands] == ["start"]


@pytest.mark.parametrize(
    "point",
    ["before-start", "start-baseline", "after-settlement", "release-baseline", "after-release"],
)
def test_cancellation_is_checked_after_blocking_baseline_and_between_phases(rig, point):
    task, backend, _, cancellation, event, _ = rig
    if point == "before-start":
        event.set()
    elif point == "start-baseline":
        calls = 0

        def cancel_second():
            nonlocal calls
            calls += 1
            if calls == 2:
                event.set()

        backend.before_observe = cancel_second
    start(task, cancellation)
    if point in {"before-start", "start-baseline"}:
        assert backend.commands == []
        return
    backend.finish()
    task.step(cancellation)
    if point == "after-settlement":
        event.set()
    elif point == "release-baseline":
        backend.before_observe = event.set
    task.step(cancellation)
    if point == "after-release":
        event.set()
    task.step(cancellation)
    assert len([a for a, _ in backend.commands if a == "start"]) == 1
    if point != "after-release":
        assert [a for a, _ in backend.commands] == ["start"]


@pytest.mark.parametrize("outcome", [CommandOutcome.UNKNOWN, CommandOutcome.REJECTED])
def test_uncertain_or_rejected_release_and_visible_idle_never_advances(rig, outcome):
    task, backend, _, cancellation, _, _ = rig
    start(task, cancellation)
    backend.finish()
    task.step(cancellation)
    backend.stop_outcome = outcome
    released = task.step(cancellation)
    assert not released.playback.release_confirmed
    task.step(cancellation)
    assert [a for a, _ in backend.commands] == ["start", "stop"]


def test_accepted_release_without_observed_idle_holds(rig):
    task, backend, _, cancellation, _, _ = rig
    start(task, cancellation)
    backend.finish()
    task.step(cancellation)
    backend.stop_idle = False
    result = task.step(cancellation)
    assert not result.playback.release_confirmed
    task.step(cancellation)
    assert [a for a, _ in backend.commands] == ["start", "stop"]


def test_reopen_completed_release_history_never_starts_pending_successor(rig):
    task, backend, store, cancellation, _, _ = rig
    start(task, cancellation)
    backend.finish()
    task.step(cancellation)
    task.step(cancellation)
    task.close()

    @contextmanager
    def factory():
        yield backend

    reopened = PlaybackTask(
        factory, store, MemoryStore(), backend.clock, queue_id="queue", target=TARGET
    )
    try:
        result = reopened.step(cancellation)
        assert result.queue is not None
        assert result.queue.cancellation_requested
        assert result.queue.items[0].intent == QueueIntent.FINISHED
        assert result.playback is None
        start(reopened, cancellation)
        assert [a for a, _ in backend.commands] == ["start", "stop"]
    finally:
        reopened.close()


@pytest.mark.parametrize("change", ["item", "content"])
def test_duplicate_start_identity_cannot_describe_different_work(rig, change):
    task, _, _, cancellation, _, _ = rig
    start(task, cancellation)
    request = PlaybackRequest("start", "attempt", ITEMS[0].item_id, ITEMS[0].content, TARGET)
    request = replace(
        request,
        **(
            {"queue_item_id": ITEMS[1].item_id}
            if change == "item"
            else {"content": ITEMS[1].content}
        ),
    )
    with pytest.raises(ValueError, match="different work"):
        task.handle(RunnerCommand("start", CommandAction.START, request), cancellation)


@pytest.mark.parametrize("hazard", ["ad", "replacement", "replay", "reset", "gap"])
def test_interference_then_idle_in_next_batch_cannot_erase_release_veto(rig, hazard):
    task, backend, _, cancellation, _, _ = rig
    start(task, cancellation)
    backend.finish()
    task.step(cancellation)
    task.step(cancellation)

    def interference():
        fields = {
            "session_id": "session",
            "application_id": "app",
            "playback_id": "media",
            "content": ITEMS[0].content,
            "state": PlayerState.BUFFERING,
        }
        if hazard == "ad":
            fields["ad_active"] = True
        elif hazard == "replacement":
            fields["content"] = ITEMS[1].content
        elif hazard == "replay":
            fields["state"] = PlayerState.PLAYING
        elif hazard == "reset":
            fields["connection_reset"] = True
        else:
            backend.sequence += 1
        return (backend.event(**fields), backend.event(session_active=False))

    backend.next_batch = interference
    result = task.step(cancellation)
    assert result.queue.items[1].intent == QueueIntent.PENDING
    task.step(cancellation)
    assert [a for a, _ in backend.commands] == ["start", "stop"]


def test_delayed_accepted_release_idle_is_observed_without_resending(rig):
    task, backend, _, cancellation, _, _ = rig
    start(task, cancellation)
    backend.finish()
    task.step(cancellation)
    backend.stop_idle = False
    assert not task.step(cancellation).playback.release_confirmed
    backend.active = False
    assert task.step(cancellation).playback.release_confirmed
    task.step(cancellation)
    assert [a for a, _ in backend.commands] == ["start", "stop", "start"]


@pytest.mark.parametrize(
    "state",
    [
        ExecutionState.PENDING,
        ExecutionState.DISPATCHED,
        ExecutionState.UNCERTAIN,
        ExecutionState.ACKNOWLEDGED,
    ],
)
def test_recovery_never_replays_release_or_rewrites_finished_attempt(rig, state):
    task, backend, store, cancellation, _, _ = rig
    start(task, cancellation)
    backend.finish()
    queue = task.step(cancellation).queue
    completed = queue.attempts[0]
    queue = store.prepare_release(
        "queue",
        attempt_id="attempt",
        command_id="release",
        at=backend.clock.utcnow(),
        expected_revision=queue.revision,
    )
    if state != ExecutionState.PENDING:
        queue = store.mark_dispatched(
            "queue", "release", at=backend.clock.utcnow(), expected_revision=queue.revision
        )
    if state in {ExecutionState.UNCERTAIN, ExecutionState.ACKNOWLEDGED}:
        queue = store.record_outcome(
            "queue", "release", state, at=backend.clock.utcnow(), expected_revision=queue.revision
        )
    task.close()

    @contextmanager
    def factory():
        yield backend

    reopened = PlaybackTask(
        factory, store, MemoryStore(), backend.clock, queue_id="queue", target=TARGET
    )
    try:
        view = reopened.step(cancellation)
        assert view.queue is not None
        assert view.queue.cancellation_requested
        assert view.queue.attempts[0] == completed
        assert backend.commands == [("start", ITEMS[0].content.content_id)]
        if state != ExecutionState.ACKNOWLEDGED:
            assert view.queue.generation > queue.generation
    finally:
        reopened.close()


def test_release_journal_permits_once_and_never_cancels_or_relabels_completion(rig):
    task, backend, store, cancellation, _, _ = rig
    start(task, cancellation)
    backend.finish()
    queue = task.step(cancellation).queue
    prepared = store.prepare_release(
        "queue",
        attempt_id="attempt",
        command_id="release",
        at=backend.clock.utcnow(),
        expected_revision=queue.revision,
    )
    assert not prepared.cancellation_requested
    assert prepared.attempts == queue.attempts
    assert (
        store.prepare_release(
            "queue",
            attempt_id="attempt",
            command_id="release",
            at=backend.clock.utcnow(),
            expected_revision=queue.revision,
        )
        == prepared
    )
    with pytest.raises(QueueStoreError):
        store.prepare_release(
            "queue",
            attempt_id="attempt",
            command_id="another-release",
            at=backend.clock.utcnow(),
            expected_revision=prepared.revision,
        )
    canceled = store.cancel_queue("queue", expected_revision=prepared.revision)
    with pytest.raises(QueueStoreError):
        store.mark_dispatched(
            "queue", "release", at=backend.clock.utcnow(), expected_revision=canceled.revision
        )


@pytest.mark.parametrize(
    "action", [CommandAction.PAUSE, CommandAction.RESUME, CommandAction.RELEASE]
)
def test_new_journal_actions_have_typed_requests(action):
    assert QueueCommandRequest("queue", "item", "attempt", "command", action, 0, 0).action == action


@pytest.mark.parametrize("outcome", list(CommandOutcome))
def test_release_idle_requires_accepted_receipt_and_later_boundary(rig, outcome):
    task, backend, _, cancellation, _, _ = rig
    start(task, cancellation)
    backend.finish()
    snapshot = task.step(cancellation).playback
    boundary = backend.clock.monotonic()
    receipt = CommandReceipt(
        "start", "attempt", CommandAction.RELEASE, outcome, backend.clock.utcnow()
    )
    snapshot = replace(snapshot, receipt=receipt, release_boundary=boundary)
    assert not snapshot.release_confirmed
    early = replace(backend.event(session_active=False), monotonic=boundary)
    assert not policy.observe(snapshot, early, now=backend.clock.monotonic()).release_confirmed
    late = replace(early, monotonic=backend.clock.tick())
    after = policy.observe(snapshot, late, now=backend.clock.monotonic())
    assert after.release_confirmed is (outcome == CommandOutcome.ACCEPTED)


def test_release_proofs_are_not_restored_from_session_storage(rig):
    task, backend, _, cancellation, _, _ = rig
    start(task, cancellation)
    backend.finish()
    task.step(cancellation)
    released = task.step(cancellation).playback
    assert released.release_confirmed
    restored = decode_snapshot(encode_snapshot(released), backend.clock)
    assert restored.release_boundary is None
    assert not restored.release_confirmed
    assert restored.completion is None


def test_stop_after_settlement_preserves_finished_outcome_and_cancels_queue(rig):
    task, backend, _, cancellation, event, _ = rig
    start(task, cancellation)
    backend.finish()
    completed = task.step(cancellation).queue.attempts[0]
    event.set()
    result = task.handle(RunnerCommand("stop", CommandAction.STOP), cancellation)
    assert result.queue.attempts[0] == completed
    assert result.queue.cancellation_requested
    assert result.playback.state == PlayerState.STOPPED
    assert not result.playback.release_confirmed


@pytest.mark.parametrize("hazard", ["replacement", "ad"])
def test_automatic_start_rechecks_predecessor_proof_after_its_own_baseline(
    rig, monkeypatch, hazard
):
    task, backend, _, cancellation, _, _ = rig
    start(task, cancellation)
    backend.finish()
    task.step(cancellation)
    task.step(cancellation)
    original = backend.observe
    calls = 0

    def later_baseline(target):
        nonlocal calls
        calls += 1
        if calls != 2:
            return original(target)
        return (
            backend.event(
                session_id="session",
                application_id="app",
                playback_id="media",
                content=ITEMS[1].content if hazard == "replacement" else ITEMS[0].content,
                ad_active=hazard == "ad",
                state=PlayerState.BUFFERING,
            ),
            backend.event(session_active=False),
        )

    monkeypatch.setattr(backend, "observe", later_baseline)
    result = task.step(cancellation)
    assert result.queue.items[1].intent == QueueIntent.NEEDS_ATTENTION
    assert result.queue.cancellation_requested
    assert [a for a, _ in backend.commands] == ["start", "stop"]


def test_refused_control_retains_replacement_from_its_blocking_baseline(rig):
    task, backend, _, cancellation, _, _ = rig
    start(task, cancellation)
    backend.next_batch = lambda: (
        backend.event(session_id="other-session", application_id="other-app", session_active=True),
        backend.event(
            session_id="other-session",
            application_id="other-app",
            content=ITEMS[1].content,
            playback_id="other-media",
            state=PlayerState.PLAYING,
        ),
    )
    refused = task.handle(RunnerCommand("pause", CommandAction.PAUSE), cancellation)
    assert refused.playback.ownership_lost
    backend.finish()
    result = task.step(cancellation)
    assert result.playback.ownership_lost
    assert result.playback.completion is None
    assert [a for a, _ in backend.commands] == ["start"]


def test_release_outcome_persistence_failure_never_uses_observed_idle_to_advance(rig, monkeypatch):
    task, backend, store, cancellation, _, _ = rig
    start(task, cancellation)
    backend.finish()
    task.step(cancellation)
    original = store.record_outcome

    def failed_outcome(queue_id, command_id, *args, **kwargs):
        if command_id.startswith("release-"):
            raise OSError("injected persistence failure")
        return original(queue_id, command_id, *args, **kwargs)

    monkeypatch.setattr(store, "record_outcome", failed_outcome)
    with pytest.raises(OutcomePersistenceError):
        task.step(cancellation)
    result = task.step(cancellation)
    assert result.playback.release_confirmed
    assert result.queue.commands[-1].state == ExecutionState.DISPATCHED
    assert result.queue.items[1].intent == QueueIntent.PENDING
    assert [a for a, _ in backend.commands] == ["start", "stop"]


def test_release_snapshot_persistence_failure_holds_even_with_accepted_remote_receipt(
    rig, monkeypatch
):
    task, backend, _, cancellation, _, _ = rig
    start(task, cancellation)
    backend.finish()
    task.step(cancellation)
    original = task._app.store.save

    def failed_save(snapshot, **kwargs):
        if snapshot.release_confirmed:
            raise OSError("injected session write failure")
        return original(snapshot, **kwargs)

    monkeypatch.setattr(task._app.store, "save", failed_save)
    result = task.step(cancellation)
    assert result.queue.cancellation_requested
    assert result.queue.items[1].intent == QueueIntent.PENDING
    assert [a for a, _ in backend.commands] == ["start", "stop"]
