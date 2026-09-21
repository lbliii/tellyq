"""Offline pause/resume control contracts; synthetic support is not hardware proof."""

from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import Mock

import pytest

from tellyq.application import ControlRefused, PlaybackApplication
from tellyq.cast_backend import CastBackend
from tellyq.cast_messages import normalize_message
from tellyq.controller import execute
from tellyq.domain.values import (
    CommandAction,
    CommandOutcome,
    ContentRef,
    ControlDiagnostic,
    ControlReason,
    ControlResponse,
    ControlStage,
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


@pytest.mark.parametrize("offset,allowed", [(-0.1, True), (0.0, False), (0.05, False)])
def test_new_client_reset_is_only_reconcilable_before_control_boundary(
    system, offset, allowed, monkeypatch
):
    clock, request, _, _, store, _, owned = system
    transport = ControlTransport(clock)
    transport.playing = True
    transport.ad = None
    observe = transport.observe
    boundary = clock.monotonic()
    first = True

    def queued(seconds):
        nonlocal first
        events = observe(seconds)
        if first:
            first = False
            events.insert(
                0, {"kind": "error", "type": "CONNECTION_RESET", "monotonic": boundary + offset}
            )
        return events

    monkeypatch.setattr(transport, "observe", queued)
    app = PlaybackApplication(CastBackend(transport, request.target, clock), store, clock)
    if allowed:
        result = app.pause(request, owned)
        assert result.control_observed
        assert result.snapshot is not None and result.snapshot.latest is not None
        assert result.snapshot.scope.connection_generation != owned.scope.connection_generation
        assert result.snapshot.latest.ad_active is None
        assert not result.snapshot.evidence.receiver_playback_confirmed
        assert len(transport.controls) == 1 and transport.plays == []
    else:
        with pytest.raises(ControlRefused) as raised:
            app.pause(request, owned)
        assert raised.value.diagnostic.reason == ControlReason.CONNECTION_CHANGED
        assert not transport.controls


@pytest.mark.parametrize("change", ["session", "content", "missing", "stale", "no_receiver"])
def test_preboundary_reset_still_requires_fresh_exact_ownership(system, change, monkeypatch):
    clock, request, _, _, store, _, owned = system
    transport = ControlTransport(clock)
    transport.playing = True
    observe = transport.observe
    boundary = clock.monotonic()
    if change == "session":
        transport.session = "replacement"
    if change == "content":
        transport.content = "replacement-video"
    if change == "missing":
        transport.media = False

    def queued(seconds):
        events = observe(seconds)
        if change == "stale":
            clock.tick(20)
        if change == "no_receiver":
            events = [event for event in events if event["kind"] != "receiver"]
        return [{"kind": "error", "type": "CONNECTION_RESET", "monotonic": boundary - 0.1}, *events]

    monkeypatch.setattr(transport, "observe", queued)
    app = PlaybackApplication(CastBackend(transport, request.target, clock), store, clock)
    with pytest.raises(ControlRefused):
        app.pause(request, owned)
    assert not transport.controls


@pytest.mark.parametrize("timing", ["aged_during_observation", "future", "out_of_order"])
def test_reset_after_boundary_is_not_excused_by_freshness_or_event_order(system, timing):
    clock, request, transport, backend, _, app, owned = system
    observe = backend.observe
    boundary = clock.monotonic()

    def changed(target):
        events = observe(target)
        instant = clock.monotonic() + 1 if timing == "future" else boundary + 0.05
        reset = replace(events[0], connection_reset=True, monotonic=instant)
        if timing == "aged_during_observation":
            clock.tick(10)
        return (*events, reset)

    backend.observe = changed
    with pytest.raises(ControlRefused) as raised:
        app.pause(request, owned)
    assert raised.value.diagnostic == ControlDiagnostic(
        ControlStage.APPLICATION, ControlReason.CONNECTION_CHANGED
    )
    assert not transport.controls


def test_buffering_is_an_application_refusal_without_command_intent(system):
    _, request, transport, _, _, app, owned = system
    transport.state = "BUFFERING"
    app.on_receipt = Mock()
    with pytest.raises(ControlRefused) as raised:
        app.pause(request, owned)
    assert raised.value.diagnostic == ControlDiagnostic(
        ControlStage.APPLICATION, ControlReason.STATE_MISMATCH
    )
    assert not transport.controls
    app.on_receipt.assert_not_called()


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
    receipt = backend.pause(owned.scope, "other-media")
    assert receipt.outcome == CommandOutcome.REJECTED
    assert receipt.diagnostic == ControlDiagnostic(
        ControlStage.BACKEND, ControlReason.IDENTITY_CHANGED
    )
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
    assert report["commands"][0]["diagnostic"] == {
        "stage": "application",
        "reason": "capability_unavailable",
    }
    assert transport.controls == []


@pytest.mark.parametrize(
    "stage,reason,outcome",
    [
        (ControlStage.TRANSPORT_GUARD, ControlReason.STATE_MISMATCH, CommandOutcome.REJECTED),
        (ControlStage.RECEIVER, ControlReason.INVALID_REQUEST, CommandOutcome.REJECTED),
        (ControlStage.RECEIVER, ControlReason.INVALID_PLAYER_STATE, CommandOutcome.REJECTED),
        (ControlStage.TRANSPORT, ControlReason.TRANSPORT_EXCEPTION, CommandOutcome.UNKNOWN),
        (ControlStage.TRANSPORT, ControlReason.RESPONSE_UNKNOWN, CommandOutcome.UNKNOWN),
    ],
)
def test_typed_transport_provenance_survives_receipt_and_cli(
    tmp_path, stage, reason, outcome, monkeypatch
):
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
    diagnostic = ControlDiagnostic(stage, reason)
    monkeypatch.setattr(transport, "control", lambda *_: ControlResponse(outcome, diagnostic))
    report, code = run("pause")
    assert code == 1 and not report["control_observed"]
    assert report["commands"][0]["outcome"] == outcome
    assert report["commands"][0]["diagnostic"] == {"stage": stage, "reason": reason}
    assert report["error"]["diagnostic"] == report["commands"][0]["diagnostic"]


def test_application_guard_provenance_survives_cli_without_a_command(tmp_path):
    clock = FakeClock()
    transport = ControlTransport(clock)
    device = "00000000-0000-4000-8000-000000000001"

    @contextmanager
    def factory(target, clock, seconds):
        yield CastBackend(transport, target, clock, seconds), {"uuid": device, "name": "synthetic"}

    def run(command, **kwargs):
        return execute(command, tmp_path, clock=clock, backend_factory=factory, **kwargs)

    run("queue", device_id=device)
    run("start")
    transport.state = "BUFFERING"
    report, code = run("pause")
    assert code == 1 and report["commands"] == []
    assert report["error"]["diagnostic"] == {"stage": "application", "reason": "state_mismatch"}
    assert transport.controls == []


@pytest.mark.parametrize("action", [CommandAction.START, CommandAction.STOP])
def test_noncontrol_receipt_json_shape_is_unchanged(action):
    from tellyq.controller import _wire_receipt
    from tellyq.domain.values import CommandReceipt

    receipt = CommandReceipt(
        "request", "attempt", action, CommandOutcome.ACCEPTED, FakeClock().utcnow()
    )
    assert set(_wire_receipt(receipt)) == {
        "action",
        "requested_at",
        "recorded_at",
        "returned",
        "outcome",
    }


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
