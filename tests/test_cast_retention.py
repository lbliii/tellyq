"""Persistent observation retains takeover proof without accumulating every packet."""

from dataclasses import replace
from itertools import product
from random import Random

import pytest

from tellyq.cast_backend import CastBackend
from tellyq.domain.values import (
    CommandOutcome,
    ContentRef,
    PlaybackObservation,
    PlaybackRequest,
    PlaybackScope,
    PlaybackTarget,
)
from tests.playback_support import FakeClock, FakeTransport

TARGET = PlaybackTarget("synthetic", "cast")


def test_compacted_identity_witnesses_match_full_history_for_every_scope_boundary():
    clock = FakeClock()
    backend = CastBackend(FakeTransport(clock), TARGET, clock)
    history = []
    random = Random(14)
    contents = [None, ContentRef("youtube", "one"), ContentRef("youtube", "two")]
    for sequence in range(100):
        event = PlaybackObservation(
            TARGET,
            backend.generation,
            sequence,
            clock.utcnow(),
            clock.tick(),
            session_id=random.choice([None, "session-a", "session-b"]),
            application_id=random.choice([None, "app-a", "app-b"]),
            content=random.choice(contents),
        )
        history.append(event)
        backend._remember_identity(event)
        assert len(backend._identity_history) <= 6

        def mismatch(events, expected, boundary):
            return any(
                observation.monotonic > boundary
                and any(
                    actual is not None and actual != wanted
                    for actual, wanted in zip(
                        (observation.session_id, observation.application_id, observation.content),
                        expected,
                        strict=True,
                    )
                )
                for observation in events
            )

        if sequence % 10 == 0:
            for expected in product(
                [None, "session-a", "session-b", "unseen"],
                [None, "app-a", "app-b", "unseen"],
                contents,
            ):
                for boundary in (
                    99.0,
                    *[item.monotonic for item in history[::10]],
                    clock.monotonic() + 1,
                ):
                    assert mismatch(backend._identity_history, expected, boundary) == mismatch(
                        history, expected, boundary
                    )


def test_repeated_same_identity_does_not_erase_takeover_before_stop():
    clock = FakeClock()
    transport = FakeTransport(clock)
    backend = CastBackend(transport, TARGET, clock, retain_raw=False)
    request = PlaybackRequest(
        "request", "attempt", "item", ContentRef("youtube", transport.content), TARGET
    )
    boundary = clock.monotonic()
    backend.start(request)
    backend.observe(TARGET)
    scope = PlaybackScope(request, transport.session, backend.generation, boundary, "youtube")
    transport.content = "replacement"
    backend.observe(TARGET)
    new_boundary = clock.monotonic()
    transport.content = request.content.content_id
    for _ in range(100):
        backend.observe(TARGET)
    assert len(backend._identity_history) <= 6
    assert len(backend.raw_observations) == 3
    assert backend.stop(scope).outcome == CommandOutcome.REJECTED
    assert transport.quits == 0
    assert (
        backend.stop(replace(scope, started_monotonic=new_boundary)).outcome
        == CommandOutcome.ACCEPTED
    )
    assert transport.quits == 1


def test_legacy_raw_capture_still_accumulates_until_explicit_drain():
    clock = FakeClock()
    transport = FakeTransport(clock)
    backend = CastBackend(transport, TARGET, clock)
    backend.observe(TARGET)
    backend.observe(TARGET)
    assert len(backend.raw_observations) == 2
    assert len(backend.drain_raw_observations()) == 2
    assert backend.raw_observations == []


def test_persistent_raw_retention_also_bounds_interrupted_observation_drain():
    clock = FakeClock()

    class Interrupted(FakeTransport):
        def observe(self, seconds):
            raise TimeoutError("synthetic timeout")

        def drain_pending(self):
            return [{"kind": "receiver", "monotonic": clock.tick(), "app_id": None}]

    backend = CastBackend(Interrupted(clock), TARGET, clock, retain_raw=False)
    for _ in range(10):
        with pytest.raises(TimeoutError):
            backend.observe(TARGET)
        assert len(backend.raw_observations) == 1


def test_connection_reset_discards_old_witnesses_and_rejects_old_scope():
    clock = FakeClock()

    class Resetting(FakeTransport):
        reset = False

        def observe(self, seconds):
            reset = (
                [{"kind": "error", "type": "CONNECTION_RESET", "monotonic": clock.tick()}]
                if self.reset
                else []
            )
            return reset + super().observe(seconds)

    transport = Resetting(clock)
    backend = CastBackend(transport, TARGET, clock, retain_raw=False)
    request = PlaybackRequest(
        "request", "attempt", "item", ContentRef("youtube", transport.content), TARGET
    )
    scope = PlaybackScope(
        request, transport.session, backend.generation, clock.monotonic(), "youtube"
    )
    backend.start(request)
    backend.observe(TARGET)
    old_generation = backend.generation
    transport.reset = True
    backend.observe(TARGET)
    assert backend.generation != old_generation
    assert backend._identity_history
    assert all(
        event.connection_generation == backend.generation for event in backend._identity_history
    )
    assert backend.stop(scope).outcome == CommandOutcome.REJECTED
    assert transport.quits == 0
