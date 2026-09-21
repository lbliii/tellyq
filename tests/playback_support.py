"""Offline transports and independent backend double for shared contracts."""

from dataclasses import replace
from datetime import UTC, datetime

from tellyq.domain.values import (
    CommandAction,
    CommandOutcome,
    CommandReceipt,
    ContentRef,
    PlaybackCapabilities,
    PlaybackObservation,
    PlaybackTarget,
    PlayerState,
)
from tellyq.models import Observation


class FakeClock:
    def __init__(self):
        self.instant = 100.0

    def utcnow(self):
        return datetime(2026, 1, 1, tzinfo=UTC)

    def monotonic(self):
        return self.instant

    def tick(self, seconds=0.1):
        self.instant += seconds
        return self.instant


class FakeTransport:
    def __init__(self, clock):
        self.clock = clock
        self.playing = False
        self.stopped = False
        self.session = "session"
        self.content = "FozIp7Va7dY"
        self.ad = False
        self.position = 10.0
        self.state = "PLAYING"
        self.media = True
        self.start_error = False
        self.stop_error = False
        self.silent_start = False
        self.silent_stop = False
        self.plays = []
        self.quits = 0

    def play(self, content_id):
        self.plays.append(content_id)
        if self.start_error:
            raise TimeoutError("private wire detail must not escape")
        if not self.silent_start:
            self.playing = True
            self.stopped = False

    def quit(self):
        self.quits += 1
        if self.stop_error:
            raise TimeoutError("private wire detail must not escape")
        if not self.silent_stop:
            self.stopped = True

    def observe(self, seconds: float) -> list[Observation]:
        del seconds
        receiver: Observation = {
            "kind": "receiver",
            "monotonic": self.clock.tick(),
            "app_id": None if self.stopped else "youtube",
            "app_name": None if self.stopped else "YouTube",
            "app_session_id": None if self.stopped else self.session,
        }
        events: list[Observation] = [receiver]
        if self.playing and not self.stopped and self.media:
            for delta in (0.1, 1.2):
                self.position += delta
                events.append(
                    {
                        "kind": "media",
                        "monotonic": self.clock.tick(delta),
                        "content_id": self.content,
                        "player_state": self.state,
                        "media_session_id": 1,
                        "position": self.position,
                        "ad_break": self.ad,
                        "duration": 50.0,
                    }
                )
        return events


class FakeBackend:
    """Independent domain-only implementation, not a subclass of the Cast adapter."""

    def __init__(self, clock, target: PlaybackTarget):
        self.clock = clock
        self.target = target
        self.generation = "fake-generation"
        self.sequence = 0
        self.started = False
        self.stopped = False
        self.commands = []
        self.content = ContentRef("youtube", "FozIp7Va7dY")
        self.position = 0.0
        self.session = "session"
        self.ad = False

    def capabilities(self, target):
        assert target == self.target
        return PlaybackCapabilities()

    def start(self, request):
        self.commands.append("start")
        self.started = True
        return CommandReceipt(
            request.request_id,
            request.attempt_id,
            CommandAction.START,
            CommandOutcome.ACCEPTED,
            self.clock.utcnow(),
        )

    def observe(self, target):
        assert target == self.target
        self.sequence += 1
        receiver = PlaybackObservation(
            target,
            self.generation,
            self.sequence,
            self.clock.utcnow(),
            self.clock.tick(),
            session_id=None if self.stopped else self.session,
            application_id=None if self.stopped else "youtube",
            session_active=not self.stopped,
        )
        events = [receiver]
        if self.started and not self.stopped:
            for delta in (0.1, 1.2):
                self.sequence += 1
                self.position += delta
                events.append(
                    replace(
                        receiver,
                        sequence=self.sequence,
                        monotonic=self.clock.tick(delta),
                        session_active=None,
                        content=self.content,
                        state=PlayerState.PLAYING,
                        position=self.position,
                        ad_active=self.ad,
                        playback_id="1",
                    )
                )
        return tuple(events)

    def stop(self, scope):
        accepted = (
            scope.session_id == self.session and scope.connection_generation == self.generation
        )
        if accepted:
            self.commands.append("stop")
            self.stopped = True
        return CommandReceipt(
            scope.request.request_id,
            scope.request.attempt_id,
            CommandAction.STOP,
            CommandOutcome.ACCEPTED if accepted else CommandOutcome.REJECTED,
            self.clock.utcnow(),
        )


class MemoryStore:
    def __init__(self):
        self.records = {}
        self.active = None

    def load(self, attempt_id):
        return self.records.get(attempt_id)

    def current(self):
        return self.active

    def save(self, snapshot, *, expected_revision):
        from tellyq.domain.ports import RevisionConflict

        previous = self.load(snapshot.scope.request.attempt_id)
        revision = previous.revision if previous is not None else None
        if revision != expected_revision or (
            revision is not None and snapshot.revision <= revision
        ):
            raise RevisionConflict("changed")
        self.records[snapshot.scope.request.attempt_id] = snapshot
        self.active = snapshot
