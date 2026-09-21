"""Identical consumer contracts run against fake and Cast with mocked transport."""

from dataclasses import replace
from uuid import uuid4

import pytest

from tellyq.application import PlaybackApplication
from tellyq.cast_backend import CastBackend
from tellyq.domain.values import (
    CommandOutcome,
    ContentRef,
    PlaybackRequest,
    PlaybackScope,
    PlaybackTarget,
    PlayerState,
)
from tests.playback_support import FakeBackend, FakeClock, FakeTransport, MemoryStore


@pytest.fixture(params=["fake", "cast"])
def contract(request):
    clock = FakeClock()
    target = PlaybackTarget(str(uuid4()), "cast")
    playback = PlaybackRequest(
        "request", "attempt", "item", ContentRef("youtube", "FozIp7Va7dY"), target
    )
    backend = (
        FakeBackend(clock, target)
        if request.param == "fake"
        else CastBackend(FakeTransport(clock), target, clock)
    )
    return backend, clock, playback, MemoryStore()


def test_same_start_observe_status_stop_application_contract(contract):
    backend, clock, request, store = contract
    app = PlaybackApplication(backend, store, clock)
    baseline = backend.observe(request.target)
    assert baseline and all(event.content is None for event in baseline)
    result = app.start(request)
    assert result.receipt is not None and result.receipt.outcome == CommandOutcome.ACCEPTED
    snapshot = result.snapshot
    assert snapshot is not None and snapshot.evidence.receiver_playback_confirmed
    assert snapshot.display is None
    old_observations = result.observations
    result = app.status(request, snapshot)
    assert result.receipt is None
    assert result.snapshot is not None and result.snapshot.evidence.receiver_playback_confirmed
    assert old_observations != result.observations  # Fresh detached values, no cached old samples.
    result = app.stop(request, result.snapshot)
    assert result.snapshot is not None and result.snapshot.state == PlayerState.STOPPED
    assert not result.snapshot.evidence.natural_completion_confirmed
    assert result.snapshot.latest is not None and result.snapshot.latest.content is None
    assert result.snapshot.latest.idle_reason is None  # Never invent content cancellation.


def test_same_command_ack_is_not_observation_contract(contract):
    backend, _, request, _ = contract
    receipt = backend.start(request)
    assert receipt.outcome == CommandOutcome.ACCEPTED
    assert not hasattr(receipt, "receiver_playback_confirmed")
    assert not hasattr(receipt, "state")


def test_same_stop_requires_owned_session_and_generation_contract(contract):
    backend, clock, request, store = contract
    result = PlaybackApplication(backend, store, clock).start(request)
    assert result.snapshot is not None
    for scope in (
        replace(result.snapshot.scope, session_id="foreign"),
        replace(result.snapshot.scope, connection_generation="old-generation"),
    ):
        assert backend.stop(scope).outcome == CommandOutcome.REJECTED
    assert backend.observe(request.target)[-1].state == PlayerState.PLAYING


def test_same_unknown_ad_state_stays_unconfirmed_contract(contract):
    backend, clock, request, store = contract
    if isinstance(backend, CastBackend):
        assert isinstance(backend.transport, FakeTransport)
        backend.transport.ad = None
    else:
        backend.ad = None
    result = PlaybackApplication(backend, store, clock).start(request)
    assert result.snapshot is not None
    assert result.snapshot.state == PlayerState.PLAYING
    assert result.reason == "ad_unknown"
    assert not result.snapshot.evidence.receiver_playback_confirmed


def test_same_store_snapshot_is_immutable_and_revision_checked(contract):
    backend, clock, request, store = contract
    result = PlaybackApplication(backend, store, clock).start(request)
    assert result.snapshot is not None
    from tellyq.domain.ports import RevisionConflict

    with pytest.raises(RevisionConflict):
        store.save(result.snapshot, expected_revision=None)
    assert store.load(request.attempt_id) == result.snapshot


def test_cast_target_guard_and_unsupported_provider():
    clock = FakeClock()
    target = PlaybackTarget("synthetic", "cast")
    request = PlaybackRequest("request", "attempt", "item", ContentRef("youtube", "video"), target)
    backend = CastBackend(FakeTransport(clock), target, clock)
    with pytest.raises(ValueError, match="target"):
        backend.observe(PlaybackTarget("different", "cast"))
    assert (
        backend.start(replace(request, content=ContentRef("unsupported", "id"))).outcome
        == CommandOutcome.REJECTED
    )
    scope = PlaybackScope(request, "unknown", backend.generation, 0, "youtube")
    assert backend.stop(scope).outcome == CommandOutcome.REJECTED
