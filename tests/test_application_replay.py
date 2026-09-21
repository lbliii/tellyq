"""Lifecycle and failure replays across the application and Cast normalization boundary."""

from dataclasses import replace
from unittest.mock import Mock

import pytest

from tellyq.application import PlaybackApplication
from tellyq.cast_backend import CastBackend
from tellyq.domain.policy import observe, request_stop
from tellyq.domain.ports import RevisionConflict
from tellyq.domain.values import (
    CommandOutcome,
    ContentRef,
    PlaybackObservation,
    PlaybackRequest,
    PlaybackScope,
    PlaybackTarget,
    PlayerState,
    SessionSnapshot,
)
from tellyq.models import Observation
from tests.playback_support import FakeBackend, FakeClock, FakeTransport, MemoryStore


@pytest.fixture
def setup():
    clock = FakeClock()
    target = PlaybackTarget("synthetic", "cast")
    request = PlaybackRequest(
        "request", "attempt", "item", ContentRef("youtube", "FozIp7Va7dY"), target
    )
    backend = FakeBackend(clock, target)
    store = MemoryStore()
    return clock, request, backend, store, PlaybackApplication(backend, store, clock)


def test_launch_transition_does_not_treat_previous_app_exit_as_takeover(setup):
    clock, request, backend, _, app = setup
    original = backend.observe

    def with_transition(target):
        if not backend.started:
            return original(target)
        clock.tick()
        exit_event = PlaybackObservation(
            target,
            backend.generation,
            backend.sequence + 1,
            clock.utcnow(),
            clock.monotonic(),
            session_active=False,
        )
        backend.sequence += 1
        return (exit_event, *original(target))

    backend.observe = with_transition
    result = app.start(request)
    assert result.snapshot is not None and result.snapshot.evidence.receiver_playback_confirmed


@pytest.mark.parametrize("operation", ["start", "stop"])
def test_post_effect_observe_failure_retains_command_receipt(setup, operation):
    _, request, backend, _, app = setup
    owned = app.start(request).snapshot if operation == "stop" else None
    original = backend.observe
    calls = 0

    def fail_after_effect(target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise TimeoutError("sensitive receiver details")
        return original(target)

    backend.observe = fail_after_effect
    result = app.stop(request, owned) if operation == "stop" else app.start(request)
    assert result.receipt is not None and result.receipt.outcome == CommandOutcome.ACCEPTED
    assert result.failure is not None and "sensitive" not in result.failure.message
    assert result.snapshot is None


def test_post_command_snapshot_failure_retains_receipt(setup):
    _, request, _, store, app = setup
    store.save = Mock(side_effect=OSError("private path"))
    result = app.start(request)
    assert result.receipt is not None and result.receipt.outcome == CommandOutcome.ACCEPTED
    assert result.failure is not None


def test_pre_command_revision_failure_never_starts_device(setup):
    _, request, backend, _, app = setup
    app.start(request)
    with pytest.raises(RevisionConflict):
        app.start(request)
    assert backend.commands == ["start"]


def test_status_cas_does_not_erase_concurrent_stop_intent(setup):
    _, request, backend, store, app = setup
    owned = app.start(request).snapshot
    assert owned is not None
    original = backend.observe

    def concurrent_stop(target):
        store.save(
            replace(owned, revision=owned.revision + 1, stop_requested=True),
            expected_revision=owned.revision,
        )
        return original(target)

    backend.observe = concurrent_stop
    with pytest.raises(RevisionConflict):
        app.status(request, owned)
    assert store.current().stop_requested


@pytest.mark.parametrize(
    "change", ["stale", "duplicate", "reordered", "future", "takeover", "ad", "partial"]
)
def test_adversarial_start_replay_cannot_confirm_playback(setup, change):
    clock, request, backend, _, app = setup
    original = backend.observe

    def altered(target):
        events = original(target)
        if not backend.started:
            return events
        receiver, first, second = events
        if change == "stale":
            clock.tick(10)
        elif change == "duplicate":
            second = first
        elif change == "reordered":
            second = replace(second, sequence=first.sequence - 1)
        elif change == "future":
            second = replace(second, monotonic=clock.monotonic() + 10)
        elif change == "takeover":
            second = replace(second, session_id="other")
        elif change == "ad":
            second = replace(second, ad_active=True)
        else:
            second = replace(second, content=None)
        return receiver, first, second

    backend.observe = altered
    result = app.start(request)
    assert result.snapshot is None or not result.snapshot.evidence.receiver_playback_confirmed
    assert result.snapshot is None or not result.snapshot.evidence.natural_completion_confirmed


def test_receiver_heartbeats_order_samples_without_refreshing_media(setup):
    clock, request, _, _, _ = setup
    scope = PlaybackScope(request, "session", "generation", 0, "youtube")
    first = PlaybackObservation(
        request.target,
        "generation",
        1,
        clock.utcnow(),
        1,
        "session",
        request.content,
        PlayerState.PLAYING,
        position=1,
        ad_active=False,
    )
    snapshot = observe(SessionSnapshot(scope), first, now=1)
    heartbeat = replace(first, sequence=3, monotonic=3, content=None, session_active=True)
    snapshot = observe(snapshot, heartbeat, now=3)
    assert snapshot.latest == first
    reordered = replace(first, sequence=2, monotonic=2, position=2)
    assert observe(snapshot, reordered, now=3) == snapshot
    late_heartbeat = replace(heartbeat, sequence=4, monotonic=7)
    assert observe(snapshot, late_heartbeat, now=7).evidence.reason == "stale"


def test_explicit_exit_requires_fresh_stop_boundary_and_no_foreign_identity(setup):
    clock, request, _, _, _ = setup
    scope = PlaybackScope(request, "session", "generation", 90, "youtube")
    snapshot = request_stop(SessionSnapshot(scope), boundary=100)
    exit_event = PlaybackObservation(
        request.target, "generation", 1, clock.utcnow(), 99, session_active=False
    )
    assert observe(snapshot, exit_event, now=100).state != PlayerState.STOPPED
    exit_event = replace(exit_event, monotonic=101)
    assert observe(snapshot, exit_event, now=101).state == PlayerState.STOPPED
    for fields in (
        {"session_id": "foreign"},
        {"content": request.content},
        {"application_id": "foreign"},
    ):
        with pytest.raises(ValueError):
            replace(exit_event, **fields)
    replaced = PlaybackObservation(
        request.target,
        "generation",
        1,
        clock.utcnow(),
        101,
        "foreign",
        application_id="youtube",
        session_active=True,
    )
    snapshot = observe(snapshot, replaced, now=101)
    assert snapshot.ownership_lost
    assert (
        observe(snapshot, replace(exit_event, sequence=2, monotonic=102), now=102).state
        != PlayerState.STOPPED
    )


def receiver(clock, timestamp, session="session"):
    return {
        "kind": "receiver",
        "monotonic": timestamp,
        "observed_at": clock.utcnow().isoformat(),
        "app_id": "youtube",
        "app_session_id": session,
        "app_name": "YouTube",
    }


def media(timestamp, content="FozIp7Va7dY"):
    return {
        "kind": "media",
        "monotonic": timestamp,
        "content_id": content,
        "media_session_id": 1,
        "player_state": "PLAYING",
        "position": timestamp,
        "ad_break": False,
    }


@pytest.mark.parametrize("boundary", ["INVALID_RECEIVER_STATUS", "CONNECTION_RESET"])
def test_old_receiver_cannot_repopulate_identity_after_uncertainty_boundary(boundary):
    clock = FakeClock()
    clock.instant = 104
    transport = FakeTransport(clock)
    target = PlaybackTarget("synthetic", "cast")
    backend = CastBackend(transport, target, clock)
    before = backend.generation
    transport.observe = Mock(
        return_value=[
            receiver(clock, 100),
            {"kind": "error", "type": boundary, "monotonic": 102},
            receiver(clock, 101),
            media(103),
        ]
    )
    events = backend.observe(target)
    assert events[-1].content is None and events[-1].session_id is None
    if boundary == "CONNECTION_RESET":
        assert backend.generation != before
        assert any(event.connection_reset for event in events)


def test_cast_sticky_takeover_survives_unknown_and_reordered_media():
    clock = FakeClock()
    clock.instant = 104
    transport = FakeTransport(clock)
    target = PlaybackTarget("synthetic", "cast")
    request = PlaybackRequest(
        "request", "attempt", "item", ContentRef("youtube", "FozIp7Va7dY"), target
    )
    backend = CastBackend(transport, target, clock)
    transport.observe = Mock(
        return_value=[
            receiver(clock, 100),
            media(101, "foreign"),
            {"kind": "media", "monotonic": 103, "empty_status": True},
            media(102),
        ]
    )
    backend.observe(target)
    scope = PlaybackScope(request, "session", backend.generation, 99, "youtube")
    assert backend.stop(scope).outcome == CommandOutcome.REJECTED
    assert transport.quits == 0


def test_connection_reset_revokes_old_scope_and_playback_evidence(setup):
    clock, request, _, _, _ = setup
    transport = FakeTransport(clock)
    backend = CastBackend(transport, request.target, clock)
    app = PlaybackApplication(backend, MemoryStore(), clock)
    result = app.start(request)
    assert result.snapshot is not None and result.snapshot.evidence.receiver_playback_confirmed
    clock.tick()
    transport.observe = Mock(
        return_value=[{"kind": "error", "type": "CONNECTION_RESET", "monotonic": clock.monotonic()}]
    )
    event = backend.observe(request.target)[0]
    snapshot = observe(result.snapshot, event, now=clock.monotonic())
    assert snapshot.ownership_lost and not snapshot.evidence.receiver_playback_confirmed
    assert backend.stop(result.snapshot.scope).outcome == CommandOutcome.REJECTED


def test_raw_cast_messages_flow_through_normalization_adapter_and_policy(setup):
    from tellyq.cast_messages import normalize_message

    clock, request, _, _, _ = setup
    transport = FakeTransport(clock)
    original = transport.observe

    def raw_wire(seconds: float) -> list[Observation]:
        result: list[Observation] = []
        for event in original(seconds):
            if event["kind"] == "receiver":
                message = {
                    "type": "RECEIVER_STATUS",
                    "status": {
                        "applications": []
                        if event["app_id"] is None
                        else [
                            {
                                "appId": event["app_id"],
                                "displayName": event["app_name"],
                                "sessionId": event["app_session_id"],
                            }
                        ]
                    },
                }
            else:
                message = {
                    "type": "MEDIA_STATUS",
                    "status": [
                        {
                            "mediaSessionId": event["media_session_id"],
                            "playerState": event["player_state"],
                            "currentTime": event["position"],
                            "media": {
                                "contentId": event["content_id"],
                                "duration": event["duration"],
                                "metadata": {"title": "Synthetic title"},
                            },
                        }
                    ],
                }
            normalized = normalize_message(message)
            assert normalized is not None
            result.append(
                {
                    **normalized,
                    "monotonic": event["monotonic"],
                    "observed_at": clock.utcnow().isoformat(),
                }
            )
        return result

    transport.observe = Mock(side_effect=raw_wire)
    backend = CastBackend(transport, request.target, clock)
    app = PlaybackApplication(backend, MemoryStore(), clock)
    started = app.start(request)
    assert started.snapshot is not None and started.snapshot.state == PlayerState.PLAYING
    assert started.reason == "ad_unknown"
    assert not started.snapshot.evidence.receiver_playback_confirmed
    media_events = [event for event in started.observations if event.position is not None]
    assert len(media_events) == 2
    first, second = media_events
    assert first.position is not None and second.position is not None
    assert first.position < second.position
    assert all(event.ad_active is None for event in media_events)
    stopped = app.stop(request, started.snapshot)
    assert stopped.snapshot is not None and stopped.snapshot.state == PlayerState.STOPPED
    assert stopped.snapshot.latest is not None and stopped.snapshot.latest.idle_reason is None
    assert not stopped.snapshot.evidence.natural_completion_confirmed
