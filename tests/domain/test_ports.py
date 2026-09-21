"""Typed in-memory implementations show the actual shape of each structural port."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from tellyq.domain.policy import observe, record_receipt
from tellyq.domain.ports import Clock, PlaybackBackend, RevisionConflict, SessionStore
from tellyq.domain.values import (
    CapabilityEvidence,
    CommandAction,
    CommandOutcome,
    CommandReceipt,
    ContentRef,
    PlaybackCapabilities,
    PlaybackObservation,
    PlaybackRequest,
    PlaybackScope,
    PlaybackTarget,
    PlayerState,
    SessionSnapshot,
    Support,
)

AT = datetime(2026, 1, 1, tzinfo=UTC)


class FixedClock:
    def __init__(self, instant: float = 12.0) -> None:
        self.instant = instant

    def utcnow(self) -> datetime:
        return AT

    def monotonic(self) -> float:
        return self.instant


class MemoryStore:
    def __init__(self) -> None:
        self.values: dict[str, SessionSnapshot] = {}

    def load(self, attempt_id: str) -> SessionSnapshot | None:
        return self.values.get(attempt_id)

    def save(self, snapshot: SessionSnapshot, *, expected_revision: int | None) -> None:
        key = snapshot.scope.request.attempt_id
        previous = self.load(key)
        actual = previous.revision if previous is not None else None
        if actual != expected_revision or (actual is not None and snapshot.revision <= actual):
            raise RevisionConflict("snapshot changed")
        self.values[key] = snapshot


class ReplayBackend:
    def __init__(self, events: tuple[PlaybackObservation, ...]) -> None:
        self.events = events
        self.commands: list[CommandAction] = []
        self.started = False

    def capabilities(self, target: PlaybackTarget) -> PlaybackCapabilities:
        verified = CapabilityEvidence(Support.VERIFIED, f"fake contract for {target.route}", AT)
        return PlaybackCapabilities(
            exact_launch=verified, stop=verified, identity=verified, progress=verified
        )

    def start(self, request: PlaybackRequest) -> CommandReceipt:
        self.commands.append(CommandAction.START)
        self.started = True
        return CommandReceipt(
            request.request_id, request.attempt_id, CommandAction.START, CommandOutcome.ACCEPTED, AT
        )

    def observe(self, target: PlaybackTarget) -> tuple[PlaybackObservation, ...]:
        return tuple(event for event in self.events if event.target == target and self.started)

    def stop(self, scope: PlaybackScope) -> CommandReceipt:
        self.commands.append(CommandAction.STOP)
        request = scope.request
        return CommandReceipt(
            request.request_id, request.attempt_id, CommandAction.STOP, CommandOutcome.ACCEPTED, AT
        )


def run_replay(
    backend: PlaybackBackend,
    store: SessionStore,
    clock: Clock,
    initial: SessionSnapshot,
) -> SessionSnapshot:
    """A test consumer, not the future device owner or persistent runner."""
    store.save(initial, expected_revision=None)
    request = initial.scope.request
    assert backend.capabilities(request.target).exact_launch.support == Support.VERIFIED
    receipt = backend.start(request)
    snapshot = record_receipt(initial, receipt)
    assert receipt.recorded_at == clock.utcnow()
    assert not snapshot.evidence.receiver_playback_confirmed
    for event in backend.observe(request.target):
        snapshot = observe(snapshot, event, now=clock.monotonic())
    store.save(snapshot, expected_revision=initial.revision)
    assert store.load(request.attempt_id) == snapshot
    return snapshot


def test_structural_backends_stores_and_clocks_work_without_inheritance():
    target = PlaybackTarget("fake-device", "replay")
    content = ContentRef("fixture-provider", "opaque-content")
    request = PlaybackRequest("request", "attempt", "queue-item", content, target)
    scope = PlaybackScope(request, "session", "generation", 10.0)
    first = PlaybackObservation(
        target,
        "generation",
        1,
        AT,
        11.0,
        "session",
        content,
        PlayerState.PLAYING,
        position=0.0,
        ad_active=False,
    )
    second = replace(first, sequence=2, monotonic=12.0, position=1.0)
    backend = ReplayBackend((first, second))
    store = MemoryStore()
    snapshot = run_replay(backend, store, FixedClock(), SessionSnapshot(scope))
    assert snapshot.evidence.receiver_playback_confirmed
    assert backend.commands == [CommandAction.START]
    snapshot = record_receipt(snapshot, backend.stop(scope))
    assert snapshot.stop_requested
    assert snapshot.state == PlayerState.PLAYING  # Stop acknowledgement is not stopped evidence.
    assert backend.commands == [CommandAction.START, CommandAction.STOP]


def test_backend_port_bootstraps_without_preknown_session_or_generation():
    target = PlaybackTarget("fake-device", "replay")
    content = ContentRef("fixture-provider", "opaque-content")
    request = PlaybackRequest("request", "attempt", "queue-item", content, target)
    first = PlaybackObservation(
        target,
        "new-generation",
        1,
        AT,
        11.0,
        "new-session",
        content,
        PlayerState.PLAYING,
        position=0.0,
        ad_active=False,
    )
    second = replace(first, sequence=2, monotonic=12.0, position=1.0)
    backend: PlaybackBackend = ReplayBackend((first, second))
    clock = FixedClock(10.0)
    assert backend.observe(target) == ()
    boundary = clock.monotonic()
    receipt = backend.start(request)
    clock.instant = 12.0
    events = backend.observe(target)
    candidate = events[0]
    # This synthetic case has no baseline session and a fresh exact post-start
    # session. Real ownership reconciliation must also handle ambiguous baselines.
    assert candidate.monotonic > boundary
    assert candidate.target == target and candidate.content == content
    assert candidate.session_id is not None
    scope = PlaybackScope(request, candidate.session_id, candidate.connection_generation, boundary)
    snapshot = record_receipt(SessionSnapshot(scope), receipt)
    for event in events:
        snapshot = observe(snapshot, event, now=clock.monotonic())
    assert snapshot.evidence.receiver_playback_confirmed
    stopped = backend.stop(scope)
    assert stopped.action == CommandAction.STOP
    assert stopped.attempt_id == request.attempt_id


def test_revision_conflict_does_not_overwrite_the_stored_snapshot():
    target = PlaybackTarget("fake-device", "replay")
    request = PlaybackRequest("request", "attempt", "queue-item", ContentRef("p", "id"), target)
    initial = SessionSnapshot(PlaybackScope(request, "session", "generation", 10.0))
    store: SessionStore = MemoryStore()
    store.save(initial, expected_revision=None)
    newer = replace(initial, revision=1, stop_requested=True)
    store.save(newer, expected_revision=0)
    with pytest.raises(RevisionConflict):
        store.save(replace(initial, revision=2), expected_revision=0)
    with pytest.raises(RevisionConflict):
        store.save(initial, expected_revision=None)
    with pytest.raises(RevisionConflict):
        store.save(newer, expected_revision=1)
    assert store.load("attempt") == newer
