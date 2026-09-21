"""Deterministic callback timing through the real observer/adapter/application.

All identities and messages are synthetic. Virtual queue waits advance a local
clock; these elapsed durations are regression assertions, not LAN measurements.
"""

from collections.abc import Callable
from queue import Empty, Queue
from unittest.mock import Mock

import pytest

from tellyq.application import PlaybackApplication
from tellyq.cast import MEDIA, RECEIVER, Connection, Observer
from tellyq.cast_backend import CastBackend, _Transport
from tellyq.domain import policy
from tellyq.domain.values import (
    CommandOutcome,
    ContentRef,
    PlaybackRequest,
    PlaybackScope,
    PlaybackTarget,
    PlayerState,
    SessionSnapshot,
)
from tellyq.models import Observation
from tests.playback_support import FakeClock, MemoryStore


class ScheduledEvents(Queue[Observation]):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.scheduled: list[tuple[float, Callable[[], None]]] = []
        self.waits = []

    def later(self, delay, action):
        self.scheduled.append((self.clock.monotonic() + delay, action))
        self.scheduled.sort(key=lambda entry: entry[0])

    def get(self, block=True, timeout=None):
        try:
            return super().get(block=False)
        except Empty:
            if not block:
                raise
        assert timeout is not None
        self.waits.append(timeout)
        end = self.clock.monotonic() + timeout
        while self.scheduled and self.scheduled[0][0] <= end:
            instant, action = self.scheduled.pop(0)
            self.clock.instant = max(instant, self.clock.monotonic())
            action()
            try:
                return super().get(block=False)
            except Empty:
                pass
        self.clock.instant = end
        raise Empty


class ReceiverRig:
    def __init__(self, monkeypatch):
        self.clock = FakeClock()
        monkeypatch.setattr("tellyq.cast.monotonic", self.clock.monotonic)
        self.events = ScheduledEvents(self.clock)
        self.receiver = Observer(RECEIVER, self.events)
        self.media = Observer(MEDIA, self.events)
        self.connection = Connection.__new__(Connection)
        self.connection.events = self.events
        self.connection.receiver_observer = self.receiver
        self.cast = Mock()
        self.cast.media_controller.is_active = True
        self.connection.cast = self.cast
        self.cast.socket_client.receiver_controller.send_message.side_effect = self.poll
        self.cast.media_controller.update_status.side_effect = self.poll_media
        self.cast.quit_app.side_effect = self.quit
        self.request_ids = []
        self.stopped = False
        self.silent = False
        self.media_enabled = True
        self.content = "synthetic-video"
        self.session = "synthetic-session"
        self.after_media = None
        self.after_idle = None
        self.target = PlaybackTarget("synthetic-device", "cast")
        self.request = PlaybackRequest(
            "stop-request", "attempt", "item", ContentRef("youtube", self.content), self.target
        )
        self.backend = CastBackend(_Transport(self.connection), self.target, self.clock)
        self.store = MemoryStore()
        self.app = PlaybackApplication(self.backend, self.store, self.clock)

    def poll(self, message):
        request_id = len(self.request_ids) + 1
        message["requestId"] = request_id
        self.request_ids.append(request_id)
        if not self.silent:
            self.events.later(0.02, lambda: self.receiver_reply(request_id))

    def receiver_reply(self, request_id):
        self.receiver.receive_message(
            None,
            {
                "type": "RECEIVER_STATUS",
                "requestId": request_id,
                "status": {
                    "applications": []
                    if self.stopped
                    else [
                        {
                            "appId": "youtube",
                            "sessionId": self.session,
                            "displayName": "YouTube",
                        }
                    ]
                },
            },
        )
        if self.stopped and self.after_idle is not None:
            self.after_idle()

    def poll_media(self):
        if self.media_enabled and not self.stopped and not self.silent:
            self.events.later(0.04, self.media_reply)

    def media_reply(self, *, ad=False):
        self.media.receive_message(
            None,
            {
                "type": "MEDIA_STATUS",
                "status": [
                    {
                        "mediaSessionId": 1,
                        "playerState": "PLAYING",
                        "currentTime": 10,
                        "media": {"contentId": self.content},
                        **({"breakStatus": {"breakId": "synthetic-ad"}} if ad else {}),
                    }
                ],
            },
        )
        if self.after_media is not None:
            self.after_media()

    def quit(self, *, timeout):
        assert timeout == 10
        self.clock.tick(0.1)
        self.stopped = True

    def owned(self):
        snapshot = SessionSnapshot(
            PlaybackScope(
                self.request,
                self.session,
                self.backend.generation,
                self.clock.monotonic() - 1,
                "youtube",
            )
        )
        events = self.backend.observe_until(
            self.target, lambda batch: any(event.content == self.request.content for event in batch)
        )
        for event in events:
            snapshot = policy.observe(snapshot, event, now=self.clock.monotonic())
        self.store.save(snapshot, expected_revision=None)
        return snapshot


@pytest.fixture
def receiver(monkeypatch):
    return ReceiverRig(monkeypatch)


def test_stop_returns_at_correlated_evidence_without_fixed_window_waits(receiver):
    owned = receiver.owned()
    started = receiver.clock.monotonic()
    result = receiver.app.stop(receiver.request, owned)
    assert receiver.clock.monotonic() - started == pytest.approx(0.16)
    assert result.receipt is not None and result.receipt.outcome == CommandOutcome.ACCEPTED
    assert result.snapshot is not None and result.snapshot.state == PlayerState.STOPPED
    assert result.reason == "stop_observed"
    assert result.snapshot == receiver.store.current()
    assert result.observations[-1].session_active is False
    assert len(result.observations) == 3  # receiver + media baseline, then correlated idle
    assert receiver.receiver._status_request is None
    assert receiver.backend._window == 2
    receiver.cast.quit_app.assert_called_once_with(timeout=10)


def test_accepted_command_without_evidence_keeps_six_second_deadline(receiver):
    owned = receiver.owned()

    def quit(*, timeout):
        receiver.quit(timeout=timeout)
        receiver.silent = True

    receiver.cast.quit_app.side_effect = quit
    started = receiver.clock.monotonic()
    result = receiver.app.stop(receiver.request, owned)
    assert receiver.clock.monotonic() - started == pytest.approx(6.14)
    assert result.receipt is not None and result.receipt.outcome == CommandOutcome.ACCEPTED
    assert result.snapshot is not None and result.snapshot.state != PlayerState.STOPPED
    assert result.snapshot.stop_requested
    assert receiver.receiver._status_request is None


def test_receiver_only_baseline_does_not_return_early_using_cached_media(receiver):
    owned = receiver.owned()
    receiver.media_enabled = False
    started = receiver.clock.monotonic()
    result = receiver.app.stop(receiver.request, owned)
    assert receiver.clock.monotonic() - started == pytest.approx(2.12)
    assert result.snapshot is not None and result.snapshot.state == PlayerState.STOPPED


def test_receiver_then_fresh_replacement_media_refuses_stop(receiver):
    owned = receiver.owned()
    receiver.content = "replacement"
    with pytest.raises(ValueError, match="Different content"):
        receiver.app.stop(receiver.request, owned)
    receiver.cast.quit_app.assert_not_called()


def test_matching_baseline_followed_by_already_queued_replacement_refuses_stop(receiver):
    owned = receiver.owned()

    def replace_content():
        receiver.after_media = None
        receiver.clock.tick(0.001)
        receiver.content = "replacement"
        receiver.media_reply()

    receiver.after_media = replace_content
    with pytest.raises(ValueError, match="Different content"):
        receiver.app.stop(receiver.request, owned)
    receiver.cast.quit_app.assert_not_called()


@pytest.mark.parametrize("contradiction", ["replacement", "ad", "reset"])
def test_queued_post_stop_contradiction_revokes_idle_candidate(receiver, contradiction):
    owned = receiver.owned()

    def contradict():
        receiver.clock.tick(0.001)
        if contradiction == "reset":
            receiver.events.put(
                {"kind": "error", "type": "CONNECTION_RESET", "monotonic": receiver.clock.instant}
            )
        elif contradiction == "ad":
            receiver.media_reply(ad=True)
        else:
            receiver.stopped = False
            receiver.session = "replacement"
            receiver.receiver_reply(0)

    receiver.after_idle = contradict
    started = receiver.clock.monotonic()
    result = receiver.app.stop(receiver.request, owned)
    assert receiver.clock.monotonic() - started == pytest.approx(6.14)
    assert result.snapshot is not None and result.snapshot.state != PlayerState.STOPPED
    assert not result.snapshot.evidence.natural_completion_confirmed
    assert any(event.session_active is False for event in result.observations)


def test_late_baseline_idle_reply_cannot_become_post_stop_evidence(receiver):
    owned = receiver.owned()
    old_request = receiver.request_ids[-1]

    def quit(*, timeout):
        receiver.quit(timeout=timeout)
        receiver.silent = True
        receiver.events.later(0.01, lambda: receiver.receiver_reply(old_request))

    receiver.cast.quit_app.side_effect = quit
    started = receiver.clock.monotonic()
    result = receiver.app.stop(receiver.request, owned)
    assert receiver.clock.monotonic() - started == pytest.approx(6.14)
    assert result.snapshot is not None and result.snapshot.state != PlayerState.STOPPED
    assert receiver.backend.raw_observations[-1]["reason"] == "UNCORRELATED_APP_ABSENCE"


def test_callback_enqueued_during_ready_is_evaluated_before_return(receiver):
    receiver.silent = True
    first: Observation = {"kind": "receiver", "monotonic": receiver.clock.instant}
    second: Observation = {"kind": "error", "monotonic": receiver.clock.instant + 0.001}
    receiver.events.put(first)
    batches = []

    def ready(batch):
        batches.append(batch)
        if batch == [first]:
            receiver.clock.tick(0.001)
            receiver.events.put(second)
            return True
        return False

    started = receiver.clock.monotonic()
    assert receiver.connection.observe_until(6, ready) == [first, second]
    assert batches == [[first], [second]]
    assert receiver.clock.monotonic() - started == pytest.approx(6)


def test_callback_drained_after_retirement_can_revoke_candidate(receiver, monkeypatch):
    receiver.silent = True
    receiver.events.put({"kind": "receiver", "monotonic": receiver.clock.instant})
    cancel = receiver.receiver.cancel_status_request
    injected = False

    def retire():
        nonlocal injected
        cancel()
        if not injected:
            injected = True
            receiver.clock.tick(0.001)
            receiver.events.put({"kind": "error", "monotonic": receiver.clock.instant})

    monkeypatch.setattr(receiver.receiver, "cancel_status_request", retire)
    batches = []

    def ready(batch):
        batches.append(batch)
        return batch[-1]["kind"] == "receiver"

    started = receiver.clock.monotonic()
    result = receiver.connection.observe_until(6, ready)
    assert [event["kind"] for event in result] == ["receiver", "error"]
    assert len(batches) == 2
    assert receiver.clock.monotonic() - started == pytest.approx(6)


def test_incremental_failure_retires_poll_and_preserves_partial_diagnostics(receiver):
    def fail(_events):
        raise OSError("synthetic failure")

    with pytest.raises(OSError, match="synthetic failure"):
        receiver.backend.observe_until(receiver.target, fail)
    assert receiver.receiver._status_request is None
    assert len(receiver.backend.raw_observations) == 1
    assert receiver.backend.raw_observations[0]["kind"] == "receiver"
    assert receiver.backend._window == 2


def test_deadline_retirement_tail_and_callbacks_it_queues_are_delivered(receiver, monkeypatch):
    receiver.silent = True
    cancel = receiver.receiver.cancel_status_request

    def retire():
        cancel()
        receiver.events.put({"kind": "receiver", "monotonic": receiver.clock.instant})

    monkeypatch.setattr(receiver.receiver, "cancel_status_request", retire)
    batches = []

    def ready(batch):
        batches.append(batch)
        if batch[-1]["kind"] == "receiver":
            receiver.events.put({"kind": "error", "monotonic": receiver.clock.instant})
        return False

    started = receiver.clock.monotonic()
    result = receiver.connection.observe_until(6, ready)
    assert [event["kind"] for event in result] == ["receiver", "error"]
    assert len(batches) == 2
    assert receiver.events.empty()
    assert receiver.clock.monotonic() - started == pytest.approx(6)


def test_incremental_raw_retention_is_the_whole_window_not_only_last_batch(receiver):
    receiver.backend._retain_raw = False
    receiver.owned()
    assert [event["kind"] for event in receiver.backend.raw_observations] == ["receiver", "media"]
    receiver.backend.observe_until(receiver.target, lambda events: bool(events))
    assert [event["kind"] for event in receiver.backend.raw_observations] == ["receiver"]
