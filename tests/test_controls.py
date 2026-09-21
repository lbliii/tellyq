"""Offline pause/resume control contracts; synthetic support is not hardware proof."""

from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import Mock

import pytest

from tellyq.application import PlaybackApplication
from tellyq.cast_backend import CastBackend
from tellyq.cast_messages import normalize_message
from tellyq.controller import execute
from tellyq.domain.values import (
    CommandAction,
    CommandOutcome,
    ContentRef,
    PlaybackRequest,
    PlaybackTarget,
    PlayerState,
    Support,
)
from tests.playback_support import FakeClock, FakeTransport, MemoryStore


class ControlTransport(FakeTransport):
    def __init__(self, clock):
        super().__init__(clock)
        self.support: bool | None = True
        self.controls = []
        self.control_error = False
        self.control_reject = False
        self.silent_control = False

    def observe(self, seconds):
        events = super().observe(seconds)
        for event in events:
            if event["kind"] == "media":
                event["pause_supported"] = self.support
        return events

    def control(self, scope, playback_id, action):
        self.controls.append((scope, playback_id, action))
        if self.control_error:
            raise TimeoutError("secret wire URL")
        if self.control_reject:
            return False
        if not self.silent_control:
            self.state = "PAUSED" if action == CommandAction.PAUSE else "PLAYING"
        return True


@pytest.fixture
def system():
    clock = FakeClock()
    target = PlaybackTarget("synthetic", "cast")
    request = PlaybackRequest(
        "request", "attempt", "item", ContentRef("youtube", "FozIp7Va7dY"), target
    )
    transport = ControlTransport(clock)
    backend = CastBackend(transport, target, clock)
    store = MemoryStore()
    app = PlaybackApplication(backend, store, clock)
    owned = app.start(request).snapshot
    assert owned is not None
    return clock, request, transport, backend, store, app, owned


def test_pause_resume_preserve_evidence_history_and_never_launch(system):
    _, request, transport, backend, _, app, owned = system
    assert backend.capabilities(request.target).pause.support == Support.ADVERTISED
    assert owned.has_confirmed_playback
    paused = app.pause(request, owned)
    assert paused.receipt.outcome == CommandOutcome.ACCEPTED
    assert paused.snapshot.state == PlayerState.PAUSED
    assert paused.control_observed
    assert paused.snapshot.has_confirmed_playback
    assert not paused.snapshot.stop_requested
    assert not paused.snapshot.evidence.natural_completion_confirmed
    resumed = app.resume(request, paused.snapshot)
    assert resumed.control_observed and resumed.snapshot.state == PlayerState.PLAYING
    assert transport.plays == [request.content.content_id]
    before = list(transport.controls)
    app.status(request, resumed.snapshot)
    assert transport.controls == before


@pytest.mark.parametrize("support", [None, False])
def test_unknown_or_unsupported_capability_refuses_effect(system, support):
    _, request, transport, _, _, app, owned = system
    transport.support = support
    result = app.pause(request, owned)
    assert result.receipt.outcome == CommandOutcome.REJECTED
    assert result.receipt.error.code == "unsupported_capability"
    assert not transport.controls


def test_optional_controls_port_is_not_required_of_existing_backend(system):
    clock, request, _, _, store, _, owned = system
    transport = FakeTransport(clock)
    transport.playing = True
    app = PlaybackApplication(CastBackend(transport, request.target, clock), store, clock)
    result = app.pause(request, owned)
    assert result.receipt is not None
    assert result.receipt.outcome == CommandOutcome.REJECTED


@pytest.mark.parametrize("change", ["session", "content", "missing", "stale", "reset"])
def test_bad_ownership_refuses_controls(system, change):
    clock, request, transport, _, _, app, owned = system
    original = transport.observe
    if change == "session":
        transport.session = "someone-else"
    if change == "content":
        transport.content = "another-video"

    def changed(seconds):
        events = original(seconds)
        if change == "missing":
            for event in events:
                if event["kind"] == "media":
                    event.pop("content_id", None)
        if change == "stale":
            clock.tick(20)
        if change == "reset":
            events.append({"kind": "error", "type": "CONNECTION_RESET", "monotonic": clock.tick()})
        return events

    transport.observe = changed
    with pytest.raises(ValueError):
        app.pause(request, owned)
    assert not transport.controls


def test_unknown_ad_does_not_prevent_owned_controls_or_turn_into_no_ad(system):
    _, request, transport, _, _, app, owned = system
    transport.ad = None
    result = app.pause(request, owned)
    assert result.control_observed
    assert result.snapshot.latest.ad_active is None
    assert result.snapshot.evidence.reason == "ad_unknown"
    assert not result.snapshot.evidence.receiver_playback_confirmed


@pytest.mark.parametrize(
    "flag,outcome",
    [
        ("control_error", CommandOutcome.UNKNOWN),
        ("control_reject", CommandOutcome.REJECTED),
        ("silent_control", CommandOutcome.ACCEPTED),
    ],
)
def test_receipt_is_not_observed_control(system, flag, outcome):
    _, request, transport, _, _, app, owned = system
    setattr(transport, flag, True)
    result = app.pause(request, owned)
    assert result.receipt.outcome == outcome
    assert not result.control_observed
    assert result.snapshot.state == PlayerState.PLAYING
    assert "secret" not in str(result.receipt)


def test_preexisting_stop_intent_refuses_resume(system):
    _, request, transport, _, _, app, owned = system
    with pytest.raises(ValueError):
        app.resume(request, replace(owned, stop_requested=True))
    assert not transport.controls


def test_intent_journal_failure_prevents_effect(system):
    _, request, transport, _, _, app, owned = system
    app.on_receipt = Mock(side_effect=OSError("local private path"))
    with pytest.raises(OSError):
        app.pause(request, owned)
    assert not transport.controls


def test_postdispatch_observation_failure_preserves_receipt(system):
    _, request, transport, backend, _, app, owned = system
    original = backend.observe

    def fail(target):
        if transport.controls:
            raise OSError("local private path")
        return original(target)

    backend.observe = fail
    result = app.pause(request, owned)
    assert result.receipt.outcome == CommandOutcome.ACCEPTED
    assert result.failure is not None
    assert not result.control_observed


@pytest.mark.parametrize(
    "value,expected",
    [
        (1, True),
        (15, True),
        (0, False),
        (2, False),
        (True, None),
        (-1, None),
        ("1", None),
        (1.0, None),
        (None, None),
    ],
)
def test_support_bit_is_strictly_validated(value, expected):
    event = normalize_message(
        {"type": "MEDIA_STATUS", "status": [{"supportedMediaCommands": value}]}
    )
    assert event is not None
    assert event["pause_supported"] is expected


def test_controller_controls_json_and_preserves_queue_contract(tmp_path):
    clock = FakeClock()
    transport = ControlTransport(clock)
    device = "00000000-0000-4000-8000-000000000001"

    @contextmanager
    def factory(target, clock, seconds):
        yield CastBackend(transport, target, clock, seconds), {"uuid": device, "name": "synthetic"}

    def run(command, **kwargs):
        return execute(command, tmp_path, clock=clock, backend_factory=factory, **kwargs)

    assert run("queue", device_id=device)[1] == 0
    assert run("start")[1] == 0
    transport.ad = None
    pause, code = run("pause")
    assert code == 0 and pause["commands"][0]["action"] == "pause"
    assert pause["observed_state"] == "paused" and pause["control_observed"]
    assert pause["state"] == "unconfirmed"
    assert pause["capabilities"]["pause"] == "advertised"
    resume, code = run("resume")
    assert code == 0 and resume["commands"][0]["action"] == "resume"
    assert resume["control_observed"] and resume["state"] == "unconfirmed"
    assert len(transport.plays) == 1


def test_rejected_command_with_external_pause_is_not_control_verified(system):
    _, request, transport, _, _, app, owned = system

    def external_pause(*_):
        transport.state = "PAUSED"
        return False

    transport.control = external_pause
    result = app.pause(request, owned)
    assert result.receipt.outcome == CommandOutcome.REJECTED
    assert result.snapshot.state == PlayerState.PAUSED
    assert not result.control_observed


def test_ambiguous_command_reply_stays_unknown(system):
    _, request, transport, _, _, app, owned = system
    transport.control = lambda *_: None
    result = app.pause(request, owned)
    assert result.receipt.outcome == CommandOutcome.UNKNOWN
    assert result.receipt.error.uncertain
    assert not result.control_observed


def test_direct_adapter_controls_refuse_retired_media_cache(system):
    _, request, transport, backend, _, _, owned = system
    transport.content = ""
    backend.observe(request.target)
    receipt = backend.pause(owned.scope, "1")
    assert receipt.outcome == CommandOutcome.REJECTED
    assert not transport.controls


def test_direct_adapter_controls_refuse_changed_media_id(system):
    _, _, transport, backend, _, _, owned = system
    assert backend.pause(owned.scope, "other-media").outcome == CommandOutcome.REJECTED
    assert not transport.controls


def test_direct_adapter_controls_refuse_effects_when_guard_disallows(system):
    _, _, transport, backend, _, _, owned = system
    backend._allows_effects = lambda: False
    assert backend.pause(owned.scope, "1").outcome == CommandOutcome.REJECTED
    assert not transport.controls


@pytest.mark.parametrize("command", ["pause", "resume"])
def test_cli_accepts_control_vocabulary(tmp_path, monkeypatch, capsys, command):
    import json

    from tellyq import __main__ as cli

    dispatch = Mock(return_value=({"command": command}, 0))
    monkeypatch.setattr(cli, "execute", dispatch)
    monkeypatch.setattr(cli, "RUNTIME", tmp_path)
    assert cli.main([command]) == 0
    assert json.loads(capsys.readouterr().out)["command"] == command
    assert dispatch.call_args.args[0] == command


def test_controller_unknown_support_rejects_without_control_effect(tmp_path):
    clock = FakeClock()
    transport = ControlTransport(clock)
    transport.support = None
    device = "00000000-0000-4000-8000-000000000001"

    @contextmanager
    def factory(target, clock, seconds):
        yield CastBackend(transport, target, clock, seconds), {"uuid": device, "name": "synthetic"}

    def run(command, **kwargs):
        return execute(command, tmp_path, clock=clock, backend_factory=factory, **kwargs)

    assert run("queue", device_id=device)[1] == 0
    assert run("start")[1] == 0
    report, code = run("pause")
    assert code == 1 and report["commands"][0]["outcome"] == "rejected"
    assert report["error"]["type"] == "CommandRejected"
    assert report["control_observed"] is False
    assert transport.controls == []


def test_control_snapshot_conflict_is_checked_before_device_effect(system):
    from tellyq.domain.ports import RevisionConflict

    _, request, transport, _, store, app, owned = system
    store.save = Mock(side_effect=RevisionConflict("changed"))
    with pytest.raises(RevisionConflict):
        app.pause(request, owned)
    assert transport.controls == []


def test_read_only_backend_cannot_launch_stop_pause_or_resume(system):
    clock, request, transport, _, _, _, _ = system
    backend = CastBackend(transport, request.target, clock, read_only=True)
    events = backend.observe(request.target)
    from tellyq.domain.values import PlaybackScope

    scope = PlaybackScope(request, "session", backend.generation, 100, "youtube")
    assert events and backend.read_only
    before = list(transport.plays)
    assert backend.start(request).outcome == CommandOutcome.REJECTED
    assert backend.stop(scope).outcome == CommandOutcome.REJECTED
    assert backend.pause(scope, "1").outcome == CommandOutcome.REJECTED
    transport.state = "PAUSED"
    backend.observe(request.target)
    assert backend.resume(scope, "1").outcome == CommandOutcome.REJECTED
    assert transport.plays == before and not transport.controls and transport.quits == 0
    property_name = "read_only"
    with pytest.raises(AttributeError):
        setattr(backend, property_name, False)


def test_missing_postcommand_session_identity_cannot_verify_control(system):
    _, request, transport, backend, _, app, owned = system
    original = backend.observe

    def unknown_identity(target):
        events = original(target)
        if transport.controls:
            return tuple(
                replace(event, session_id=None) if event.content else event for event in events
            )
        return events

    backend.observe = unknown_identity
    result = app.pause(request, owned)
    assert result.receipt.outcome == CommandOutcome.ACCEPTED
    assert not result.control_observed
    assert result.snapshot.state == PlayerState.UNKNOWN
