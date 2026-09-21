"""Synthetic lifecycle replays; these are not hardware acceptance evidence."""

import json
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tellyq.cast_backend import CastBackend
from tellyq.domain.values import (
    CommandOutcome,
    ContentRef,
    IdleReason,
    PlaybackObservation,
    PlaybackRequest,
    PlaybackScope,
    PlaybackTarget,
    PlayerState,
)
from tellyq.lifecycle import (
    CaptureSanitizer,
    JsonlJournal,
    LifecycleTracker,
    capture_lifecycle,
    export_capture,
    replay_capture,
)
from tests.playback_support import FakeTransport

TARGET = PlaybackTarget("private-device", "cast")
CONTENT = ContentRef("youtube", "video-id")
REQUEST = PlaybackRequest("request", "attempt", "item", CONTENT, TARGET)


class Clock:
    def __init__(self):
        self.instant = 100.0

    def monotonic(self):
        return self.instant

    def utcnow(self):
        return datetime(2026, 9, 21, tzinfo=UTC) + timedelta(seconds=self.instant - 100)

    def tick(self, seconds=0.1):
        self.instant += seconds
        return self.instant


class Sink:
    def __init__(self):
        self.records = []

    def write(self, record):
        self.records.append(record)


def observation(instant, *, sequence=None, **changes):
    base = PlaybackObservation(
        TARGET,
        "private-generation",
        sequence if sequence is not None else int(instant * 10),
        datetime(2026, 9, 21, tzinfo=UTC),
        instant,
        session_id="private-session",
        application_id="private-app",
        content=CONTENT,
        playback_id="private-playback",
        state=PlayerState.PLAYING,
        position=max(0, instant - 100),
        duration=5,
        ad_active=False,
    )
    return replace(base, **changes)


def replay(events):
    tracker = LifecycleTracker(REQUEST, 100)
    for event in events:
        tracker.observe((event,), now=event.monotonic)
    return tracker


def test_continuous_replay_requires_progress_then_explicit_natural_end():
    first = observation(101)
    second = observation(102.1)
    end = observation(103, state=PlayerState.IDLE, idle_reason=IdleReason.FINISHED)
    tracker = replay([first, second, end])
    assert tracker.evidence()["natural_completion_confirmed"] is True
    assert tracker.snapshot.state == PlayerState.ENDED
    revision = tracker.snapshot.revision
    tracker.observe([end, second], now=103)
    assert tracker.snapshot.revision == revision
    assert replay([end]).evidence()["natural_completion_confirmed"] is False


@pytest.mark.parametrize("ad", [True, None])
def test_ads_and_unknown_cross_duration_without_completion(ad):
    events = [observation(101), observation(102.1)]
    events += [observation(instant, ad_active=ad) for instant in (104, 106, 108)]
    events.append(
        observation(109, state=PlayerState.IDLE, idle_reason=IdleReason.FINISHED, ad_active=ad)
    )
    tracker = replay(events)
    assert tracker.evidence()["natural_completion_confirmed"] is False
    assert tracker.snapshot.state != PlayerState.ENDED
    assert tracker.evidence()["reason"] == ("ad_active" if ad else "ad_unknown")


def test_long_pause_buffering_and_elapsed_runtime_do_not_finish():
    events = [observation(101), observation(102.1)]
    events += [
        observation(instant, state=PlayerState.PAUSED, position=2.1)
        for instant in range(104, 130, 2)
    ]
    events += [
        observation(instant, state=PlayerState.BUFFERING, position=2.1)
        for instant in (130, 132, 134)
    ]
    tracker = replay(events)
    assert tracker.snapshot.state == PlayerState.BUFFERING
    assert tracker.evidence()["natural_completion_confirmed"] is False
    tracker.observe([observation(136), observation(137.1)], now=137.1)
    tracker.observe(
        [observation(138, state=PlayerState.IDLE, idle_reason=IdleReason.FINISHED)], now=138
    )
    assert tracker.evidence()["natural_completion_confirmed"] is True


@pytest.mark.parametrize(
    "changes",
    [
        {"content": ContentRef("youtube", "provider-autoplay")},
        {"session_id": "another-controller"},
        {"connection_reset": True},
        {"session_active": False, "session_id": None, "application_id": None, "content": None},
    ],
)
def test_takeover_disconnect_and_app_exit_do_not_finish_or_reattach(changes):
    tracker = replay(
        [
            observation(101),
            observation(102.1),
            observation(103, **changes),
            observation(104),
            observation(105.1),
            observation(106, state=PlayerState.IDLE, idle_reason=IdleReason.FINISHED),
        ]
    )
    assert tracker.snapshot.ownership_lost
    assert tracker.evidence()["natural_completion_confirmed"] is False
    assert tracker.snapshot.scope.session_id == "private-session"


def test_manual_cancel_error_and_missing_idle_reason_are_not_natural_end():
    for reason in (IdleReason.CANCELED, IdleReason.ERROR, None):
        tracker = replay(
            [
                observation(101),
                observation(102.1),
                observation(103, state=PlayerState.IDLE, idle_reason=reason),
            ]
        )
        assert tracker.evidence()["natural_completion_confirmed"] is False


def test_scope_selects_fresh_exact_content_once():
    tracker = LifecycleTracker(REQUEST, 100)
    tracker.observe(
        [
            observation(99),
            observation(106),  # future at now105
            observation(101, target=PlaybackTarget("other", "youtube-cast")),
            observation(102, application_id=None),
            observation(103, content=None),
            observation(104, session_id=None),
        ],
        now=105,
    )
    assert tracker.snapshot is None
    tracker.observe([observation(106)], now=112)  # stale
    assert tracker.snapshot is None
    tracker.observe([observation(113)], now=113)
    assert tracker.snapshot is not None


class ReplayBackend:
    def __init__(self, clock, events=(), *, failure=None):
        self.clock = clock
        self.events = list(events)
        self.failure = failure
        self.raw = []
        self.windows = []

    def observe_window(self, target, seconds):
        assert target == TARGET
        self.windows.append(seconds)
        self.clock.tick(seconds)
        if self.failure is not None:
            self.raw.append(
                {"kind": "error", "type": "CONNECTION_RESET", "monotonic": self.clock.monotonic()}
            )
            raise self.failure
        batch = tuple(event for event in self.events if event.monotonic <= self.clock.monotonic())
        self.events = [event for event in self.events if event not in batch]
        self.raw = [
            {
                "kind": "media",
                "monotonic": event.monotonic,
                "ad_break": event.ad_active,
                "content_id": CONTENT.content_id,
            }
            for event in batch
        ]
        return batch

    def drain_raw_observations(self):
        result, self.raw = self.raw, []
        return result


def test_bounded_capture_writes_gaps_and_never_promotes_timeout():
    clock, sink = Clock(), Sink()
    backend = ReplayBackend(clock, [observation(101), observation(102.1)])
    end = capture_lifecycle(
        backend,
        target=TARGET,
        content=CONTENT,
        clock=clock,
        sink=sink,
        seconds=10.5,
        provenance="synthetic",
    )
    assert end["stop_reason"] == "deadline"
    assert end["state"] == "unknown"
    assert not end["evidence"]["natural_completion_confirmed"]
    assert all(value["entire_run_observed"] is False for value in sink.records)
    assert all(value["external_control_unobserved"] is True for value in sink.records)
    assert any(value["kind"] == "gap" for value in sink.records)
    assert max(backend.windows) <= 2 and backend.windows[-1] == 0.5
    assert [record["sequence"] for record in sink.records] == list(range(len(sink.records)))
    assert backend.raw == []


@pytest.mark.parametrize(
    "failure,reason",
    [(KeyboardInterrupt(), "interrupted"), (TimeoutError("SECRET device URL"), "backend_error")],
)
def test_interrupted_or_failed_capture_preserves_tail_and_safe_end(failure, reason):
    clock, sink = Clock(), Sink()
    end = capture_lifecycle(
        ReplayBackend(clock, failure=failure),
        target=TARGET,
        content=CONTENT,
        clock=clock,
        sink=sink,
        seconds=10,
        provenance="synthetic",
    )
    assert end["stop_reason"] == reason
    assert sink.records[-2]["observations"][0]["type"] == "CONNECTION_RESET"
    assert "SECRET" not in json.dumps(sink.records)


def test_cancellation_is_local_and_does_not_observe_again():
    clock, sink = Clock(), Sink()
    backend = ReplayBackend(clock)
    end = capture_lifecycle(
        backend,
        target=TARGET,
        content=CONTENT,
        clock=clock,
        sink=sink,
        seconds=10,
        cancelled=lambda: True,
    )
    assert end["stop_reason"] == "cancelled"
    assert backend.windows == []


def test_fast_backend_waits_instead_of_busy_looping():
    clock, sink = Clock(), Sink()
    waits = []

    class Immediate(ReplayBackend):
        def observe_window(self, target, seconds):
            return ()

    def wait(seconds):
        waits.append(seconds)
        clock.tick(seconds)

    capture_lifecycle(
        Immediate(clock),
        target=TARGET,
        content=CONTENT,
        clock=clock,
        sink=sink,
        seconds=3,
        wait=wait,
    )
    assert waits == [2, 1]


@pytest.mark.parametrize("seconds", [0, -1, True, float("nan"), float("inf"), 14401])
def test_invalid_capture_limit_never_observes(seconds):
    clock, sink = Clock(), Sink()
    backend = ReplayBackend(clock)
    with pytest.raises(ValueError):
        capture_lifecycle(
            backend, target=TARGET, content=CONTENT, clock=clock, sink=sink, seconds=seconds
        )
    assert backend.windows == []
    assert sink.records == []


def test_read_only_cast_memory_is_per_window_and_effects_refused():
    clock = Clock()
    transport = FakeTransport(clock)
    transport.playing = True
    backend = CastBackend(transport, TARGET, clock, read_only=True)
    scope = PlaybackScope(REQUEST, transport.session, backend.generation, 99, "youtube")
    for _ in range(100):
        backend.observe_window(TARGET, 1.5)
        assert len(backend.raw_observations) == 3
    assert backend.start(REQUEST).outcome == CommandOutcome.REJECTED
    assert backend.stop(scope).outcome == CommandOutcome.REJECTED
    assert transport.plays == [] and transport.quits == 0
    assert len(backend.drain_raw_observations()) == 3
    assert backend.raw_observations == []
    with pytest.raises(AttributeError):
        property_name = "read_only"
        setattr(backend, property_name, False)


def test_capture_windows_require_observation_only_mode_and_short_bounds():
    clock = Clock()
    backend = CastBackend(FakeTransport(clock), TARGET, clock)
    with pytest.raises(ValueError):
        backend.observe_window(TARGET, 2)
    backend = CastBackend(FakeTransport(clock), TARGET, clock, read_only=True)
    for value in (True, 0, 2.1, float("nan")):
        with pytest.raises(ValueError):
            backend.observe_window(TARGET, value)


def test_journal_is_private_incremental_and_exclusive(tmp_path):
    clock, sink = Clock(), Sink()
    capture_lifecycle(
        ReplayBackend(clock),
        target=TARGET,
        content=CONTENT,
        clock=clock,
        sink=sink,
        seconds=1,
        provenance="synthetic",
    )
    path = tmp_path / "private.jsonl"
    with JsonlJournal(path) as journal:
        for record in sink.records:
            journal.write(record)
            assert json.loads(path.read_text().splitlines()[-1]) == record
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        JsonlJournal(path)


def private_records():
    clock, sink = Clock(), Sink()
    capture_lifecycle(
        ReplayBackend(clock), target=TARGET, content=CONTENT, clock=clock, sink=sink, seconds=1
    )
    sink.records[1]["observations"] = [
        {
            "kind": "media",
            "monotonic": 100.5,
            "observed_at": "2026-09-21T00:00:00.500000+00:00",
            "app_session_id": "SECRET session",
            "content_id": "https://youtube.com/watch?v=video-id&token=SECRET",
            "media_session_id": 99,
            "title": "SECRET title",
            "app_id": "SECRET app",
            "app_name": "YouTube",
            "player_state": "IDLE",
            "idle_reason": "INTERRUPTED",
            "ad_break": None,
            "empty_status": True,
            "position": float("nan"),
            "duration": float("inf"),
            "type": "SECRET error",
            "reason": "SECRET error detail",
            "arbitrary": {"credential": "SECRET"},
        }
    ]
    return sink.records


def test_sanitizer_replaces_ids_consistently_allowlists_scalars_and_preserves_unknown():
    records = private_records()
    sanitizer = CaptureSanitizer()
    safe = [sanitizer.record(record) for record in records]
    encoded = json.dumps(safe, allow_nan=False)
    assert "SECRET" not in encoded and "https://" not in encoded
    assert "private-device" not in encoded and "2026" not in encoded and "video-id" not in encoded
    assert all(record["provenance"] == "sanitized-live" for record in safe)
    event = safe[1]["observations"][0]
    assert event["content_id"] == safe[0]["requested_content_id"]
    assert event["ad_break"] is None and event["empty_status"] is True
    assert event["idle_reason"] == "INTERRUPTED"
    assert event["position"] is None and event["duration"] is None
    assert event["monotonic"] == 0.5
    assert event["observed_at"] == "2000-01-01T00:00:00.500000+00:00"
    assert "evidence" not in safe[1]  # Unvalidated claims must be recomputed by replay.
    assert sanitizer.observation(records[1]["observations"][0]) == event
    second = CaptureSanitizer()
    assert second.record(records[0])["capture_id"] != safe[0]["capture_id"]


def test_export_is_streamed_and_keeps_synthetic_provenance(tmp_path):
    records = private_records()
    for record in records:
        record["provenance"] = "synthetic"
    records[1]["observations"][0]["position"] = 1.0
    records[1]["observations"][0]["duration"] = 2.0
    source, destination = tmp_path / "source.jsonl", tmp_path / "fixture.jsonl"
    with JsonlJournal(source) as sink:
        for record in records:
            sink.write(record)
    export_capture(source, destination)
    assert all(
        json.loads(line)["provenance"] == "synthetic"
        for line in destination.read_text().splitlines()
    )
    assert "SECRET" not in destination.read_text()


@pytest.mark.parametrize("value", ["[]\n", "{broken}\n", "{}", "x" * 1_048_577])
def test_export_rejects_invalid_or_incomplete_lines(tmp_path, value):
    source = tmp_path / "source.jsonl"
    source.write_text(value)
    with pytest.raises(ValueError):
        export_capture(source, tmp_path / "fixture.jsonl")


def test_export_requires_begin_and_consistent_provenance():
    records = private_records()
    with pytest.raises(ValueError):
        CaptureSanitizer().record(records[1])
    sanitizer = CaptureSanitizer()
    sanitizer.record(records[0])
    records[1]["provenance"] = "synthetic"
    with pytest.raises(ValueError):
        sanitizer.record(records[1])


def test_module_and_script_help_are_offline_and_import_isolated(tmp_path):
    project = Path(__file__).resolve().parents[1]
    code = f"""
import sys
sys.path.insert(0, {str(project)!r})
def audit(event, args):
    if event.startswith("socket.") or event in {{"os.mkdir", "os.remove", "os.rename"}}:
        raise AssertionError(event)
sys.addaudithook(audit)
import tellyq.lifecycle
assert not any(name in sys.modules for name in ("pychromecast", "zeroconf", "tellyq.cast"))
"""
    subprocess.run(
        [sys.executable, "-B", "-c", code], cwd=tmp_path, check=True, capture_output=True
    )
    result = subprocess.run(
        [sys.executable, str(project / "scripts/capture_lifecycle.py"), "--help"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "capture" in result.stdout and "sanitize" in result.stdout
    assert not (tmp_path / "runtime").exists()


def test_heartbeats_foreign_samples_and_duplicates_cannot_hide_media_gap():
    clock, sink = Clock(), Sink()
    first = observation(101)
    events = [first, observation(102.1)]
    events += [
        observation(instant, content=None, session_active=True) for instant in (104, 106, 108)
    ]
    events += [observation(106.5, target=PlaybackTarget("foreign", "cast")), first]
    capture_lifecycle(
        ReplayBackend(clock, events),
        target=TARGET,
        content=CONTENT,
        clock=clock,
        sink=sink,
        seconds=9,
        provenance="synthetic",
    )
    gaps = [record for record in sink.records if record["kind"] == "gap"]
    assert [gap["gap_kind"] for gap in gaps] == ["media"]
    assert gaps[0]["gap_seconds"] > 5
    assert sink.records[-1]["evidence"]["natural_completion_confirmed"] is False


def test_watch_and_short_urls_share_requested_content_pseudonym():
    sanitizer = CaptureSanitizer()
    begin = sanitizer.record(private_records()[0])
    bare = begin["requested_content_id"]
    for content in (
        "video-id",
        "https://www.youtube.com/watch?v=video-id&secret=SECRET",
        "https://youtu.be/video-id?secret=SECRET",
    ):
        event = sanitizer.observation({"kind": "media", "content_id": content})
        assert event["content_id"] == bare
    assert (
        sanitizer.observation({"kind": "media", "content_id": "https://private.invalid/SECRET"})[
            "content_id"
        ]
        is None
    )


@pytest.mark.parametrize("ad,completed", [(False, True), (None, False)])
@pytest.mark.parametrize("partial", [False, True])
def test_exported_normalized_capture_replays_identity_and_completion(
    tmp_path, ad, completed, partial
):
    clock, sink = Clock(), Sink()
    capture_lifecycle(
        ReplayBackend(clock),
        target=TARGET,
        content=CONTENT,
        clock=clock,
        sink=sink,
        seconds=4,
        provenance="synthetic",
    )
    windows = [record for record in sink.records if record["kind"] == "window"]

    def receiver(instant):
        return {
            "kind": "receiver",
            "app_id": "private-app",
            "app_session_id": "private-session",
            "app_name": "YouTube",
            "monotonic": instant,
        }

    def media(instant, state="PLAYING"):
        return {
            "kind": "media",
            "content_id": "https://www.youtube.com/watch?v=video-id&token=SECRET",
            "player_state": state,
            "position": instant - 100,
            "duration": 4,
            "idle_reason": "FINISHED" if state == "IDLE" else None,
            "monotonic": instant,
            "ad_break": ad,
            "media_session_id": 99,
        }

    windows[0]["observations"] = [receiver(100.1), media(100.5), media(101.7)]
    windows[1]["observations"] = [receiver(102.1), media(103, "IDLE")]
    windows[1]["partial"] = partial
    source, sanitized, replayed = (
        tmp_path / name for name in ("source.jsonl", "sanitized.jsonl", "replayed.jsonl")
    )
    with JsonlJournal(source) as journal:
        for record in sink.records:
            journal.write(record)
    export_capture(source, sanitized)
    replay_capture(sanitized, replayed)
    output = [json.loads(line) for line in replayed.read_text().splitlines()]
    assert any(record["evidence"]["natural_completion_confirmed"] for record in output) is (
        completed and not partial
    )
    assert [record for record in output if record["kind"] == "window"][-1]["partial"] is partial
    assert output[-1]["attached"] is True
    assert all(record["provenance"] == "synthetic" for record in output)
    assert all(record["entire_run_observed"] is False for record in output)
    assert "SECRET" not in replayed.read_text()
    assert "private-session" not in replayed.read_text()


def test_rejected_sequence_and_lost_scope_samples_do_not_hide_media_gap():
    clock, sink = Clock(), Sink()
    events = [observation(101), observation(102.1)]
    events += [observation(instant, sequence=1) for instant in (104, 106)]
    events += [observation(107, connection_reset=True), observation(108)]
    capture_lifecycle(
        ReplayBackend(clock, events),
        target=TARGET,
        content=CONTENT,
        clock=clock,
        sink=sink,
        seconds=9,
        provenance="synthetic",
    )
    gaps = [record for record in sink.records if record["kind"] == "gap"]
    assert [gap["gap_kind"] for gap in gaps] == ["media"]
    assert sink.records[-1]["evidence"]["reason"] == "session_replaced"


@pytest.mark.parametrize(
    "failure,reason",
    [(KeyboardInterrupt(), "interrupted"), (OSError("SECRET socket path"), "backend_error")],
)
def test_actual_connection_interruption_drains_pending_normalized_tail(
    monkeypatch, failure, reason
):
    from pychromecast.controllers.receiver import ReceiverController

    from tellyq.cast import MEDIA, RECEIVER, Connection, Observer
    from tellyq.cast_backend import _Transport

    clock, sink = Clock(), Sink()
    connection = Connection.__new__(Connection)
    connection.events = Queue()
    connection.receiver_observer = Observer(RECEIVER, connection.events)
    media_observer = Observer(MEDIA, connection.events)
    receiver = Mock(spec=ReceiverController)

    def send(request):
        request["requestId"] = 41

    receiver.send_message.side_effect = send
    cast = Mock()
    cast.socket_client.receiver_controller = receiver
    cast.media_controller.is_active = True
    connection.cast = cast

    def wait(seconds):
        clock.tick(seconds)
        # Actual callback normalization and queue, followed by interruption before
        # Connection.observe reaches its normal drain() call.
        media_observer.receive_message(
            None,
            {
                "type": "MEDIA_STATUS",
                "status": [
                    {
                        "mediaSessionId": 1,
                        "playerState": "IDLE",
                        "idleReason": "FINISHED",
                        "media": {"contentId": CONTENT.content_id},
                    }
                ],
            },
        )
        raise failure

    monkeypatch.setattr("tellyq.cast.monotonic", clock.monotonic)
    monkeypatch.setattr("tellyq.cast.Event", lambda: SimpleNamespace(wait=wait))
    backend = CastBackend(_Transport(connection), TARGET, clock, read_only=True)
    end = capture_lifecycle(
        backend,
        target=TARGET,
        content=CONTENT,
        clock=clock,
        sink=sink,
        seconds=10,
        provenance="synthetic",
    )
    windows = [record for record in sink.records if record["kind"] == "window"]
    assert len(windows) == 1 and windows[0]["partial"] is True
    assert windows[0]["observations"][0]["idle_reason"] == "FINISHED"
    assert connection.events.empty()
    assert end["stop_reason"] == reason
    assert end["attached"] is False
    assert not end["evidence"]["natural_completion_confirmed"]
    assert "SECRET" not in json.dumps(sink.records)
    cast.media_controller.update_status.assert_called_once_with()
    cast.quit_app.assert_not_called()


def test_pending_drain_failure_preserves_original_exception():
    clock = Clock()
    failure = KeyboardInterrupt()

    class Faulty(FakeTransport):
        def observe(self, seconds):
            raise failure

        def drain_pending(self):
            raise OSError("secondary failure")

    backend = CastBackend(Faulty(clock), TARGET, clock, read_only=True)
    with pytest.raises(KeyboardInterrupt) as caught:
        backend.observe_window(TARGET, 2)
    assert caught.value is failure


def test_sanitizer_preserves_known_launch_failures_without_diagnostics():
    from tellyq.cast_messages import normalize_message

    normal = normalize_message(
        {"type": "LAUNCH_ERROR", "reason": "APP_NOT_FOUND", "detailedErrorCode": 4}
    )
    assert normal is not None
    payload = dict(normal)
    payload["private"] = "SECRET device location"
    safe = CaptureSanitizer().observation(payload)
    assert safe == {"kind": "error", "type": "LAUNCH_ERROR", "reason": "APP_NOT_FOUND", "code": 4}
    normal["reason"] = "SECRET arbitrary details"
    assert "reason" not in CaptureSanitizer().observation(normal)
    assert "SECRET" not in json.dumps(CaptureSanitizer().observation(normal))
