"""Migrated CLI regressions exercise injected Cast transport with honest clock samples."""

import json
from contextlib import contextmanager
from unittest.mock import Mock
from uuid import uuid4

import pytest

from tellyq.cast_backend import CastBackend
from tellyq.controller import execute
from tellyq.state import command_lock, load_queue, read, save
from tests.playback_support import FakeClock, FakeTransport


@pytest.fixture
def system(tmp_path):
    clock = FakeClock()
    transport = FakeTransport(clock)
    device_id = str(uuid4())

    @contextmanager
    def factory(target, clock, seconds):
        assert target.device_id == device_id
        yield CastBackend(transport, target, clock, seconds), {"uuid": device_id, "name": "Test"}

    def run(command, **kwargs):
        return execute(command, tmp_path, clock=clock, backend_factory=factory, **kwargs)

    return tmp_path, transport, device_id, run


def started(system):
    _, transport, device, run = system
    assert run("queue", device_id=device)[0]["state"] == "queued"
    assert not transport.plays
    report, code = run("start")
    assert code == 0
    assert report["state"] == "playing"
    return report


def test_queue_start_status_stop_and_persistence(system):
    runtime, transport, _, run = system
    report = started(system)
    assert report["schema_version"] == 1
    attempt = report["attempt_id"]
    for _ in range(2):
        report, code = run("status")
        assert code == 0 and report["commands"] == []
        assert report["attempt_id"] == attempt
        assert report["evidence"]["receiver_playback_confirmed"]
    assert len(transport.plays) == 1
    report, code = run("stop")
    assert code == 0 and report["stop_confirmed"]
    assert load_queue(runtime)["items"][0]["state"] == "stopped"
    report, code = run("status")
    assert code == 0 and report["state"] == "unconfirmed"
    assert load_queue(runtime)["items"][0]["state"] == "stopped"


def test_duplicate_start_does_not_restart_program(system):
    runtime, transport, _, run = system
    started(system)
    assert run("start")[1] == 1
    assert len(transport.plays) == 1
    assert load_queue(runtime)["items"][0]["state"] == "playing"


def test_stop_refuses_a_replacement_session(system):
    _, transport, _, run = system
    started(system)
    transport.session = "other"
    assert run("stop")[1] == 1
    assert transport.quits == 0


def test_stop_refuses_different_content(system):
    _, transport, _, run = system
    started(system)
    transport.content = "other-video"
    assert run("stop")[1] == 1
    assert transport.quits == 0


def test_command_return_without_evidence_stays_unconfirmed(system):
    _, transport, device, run = system
    run("queue", device_id=device)
    transport.silent_start = True
    report, code = run("start")
    assert code == 0 and report["commands"][0]["returned"]
    assert report["state"] == "unconfirmed"


@pytest.mark.parametrize("action", ["start", "stop"])
def test_uncertain_network_failure_is_not_success(system, action):
    runtime, transport, device, run = system
    if action == "stop":
        started(system)
        transport.stop_error = True
    else:
        run("queue", device_id=device)
        transport.start_error = True
    report, code = run(action)
    assert code == 1 and report["state"] == "unconfirmed"
    assert load_queue(runtime)["items"][0]["state"] == "unconfirmed"
    assert "private wire" not in json.dumps(report)


def test_unknown_ads_preserve_telemetry_without_verifying_playback(system):
    runtime, transport, device, run = system
    run("queue", device_id=device)
    transport.ad = None
    report, code = run("start")
    assert code == 0 and report["state"] == "unconfirmed"
    assert report["observed_state"] == "playing"
    assert report["evidence"]["reason"] == "ad_unknown"
    assert report["evidence"]["advancing_position_observed"]
    assert not report["evidence"]["receiver_playback_confirmed"]
    assert run("start")[1] == 1
    assert load_queue(runtime)["items"][0]["state"] == "unconfirmed"


def test_status_persists_uncertainty_without_restarting(system):
    runtime, transport, _, run = system
    started(system)
    transport.playing = False
    report, code = run("status")
    assert code == 0 and report["state"] == "unconfirmed"
    assert load_queue(runtime)["items"][0]["state"] == "unconfirmed"
    assert len(transport.plays) == 1


def test_stop_ack_without_exit_persists_uncertainty(system):
    runtime, transport, _, run = system
    started(system)
    transport.silent_stop = True
    report, code = run("stop")
    assert code == 0 and not report["stop_confirmed"]
    assert report["state"] == "unconfirmed"
    assert load_queue(runtime)["items"][0]["state"] == "unconfirmed"


def test_legacy_session_is_reconciled_from_fresh_evidence(system):
    runtime, transport, device, run = system
    run("queue", device_id=device)
    save(
        runtime / "session.json",
        {
            "device_id": device,
            "content_id": transport.content,
            "app_id": "youtube",
            "app_session_id": "session",
        },
    )
    transport.playing = True
    assert run("status")[0]["state"] == "playing"
    legacy = read(runtime / "session.json")
    assert legacy is not None and legacy["app_session_id"] == "session"
    assert run("stop")[0]["stop_confirmed"]


def test_invalid_state_fails_before_backend_io(system):
    runtime, _, device, run = system
    run("queue", device_id=device)
    path = runtime / "queue.json"
    data = json.loads(path.read_text())
    data["schema_version"] = 99
    path.write_text(json.dumps(data))
    backend = Mock()
    report, code = execute("start", runtime, backend_factory=backend)
    assert code == 1 and report["schema_version"] == 1
    backend.assert_not_called()


def test_runtime_duration_never_means_finished(system):
    _, transport, _, run = system
    started(system)
    transport.position = 50
    transport.state = "BUFFERING"
    assert run("status")[0]["state"] == "unconfirmed"
    transport.state = "IDLE"
    assert run("status")[0]["state"] == "unconfirmed"


def test_app_exit_invalidates_old_playing_state(system):
    _, transport, _, run = system
    started(system)
    transport.stopped = True
    assert run("status")[0]["state"] == "unconfirmed"


def test_process_lock_rejects_overlapping_commands(tmp_path):
    with command_lock(tmp_path), pytest.raises(RuntimeError), command_lock(tmp_path):
        pytest.fail("second command must not enter")


def test_saved_state_is_private_and_no_temporary_file_remains(tmp_path):
    path = tmp_path / "state.json"
    save(path, {"value": 1})
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("command", ["start", "probe", "stop"])
def test_post_effect_failure_keeps_journal_and_report_receipt(system, command):
    runtime, transport, device, run = system
    if command == "stop":
        started(system)
    elif command == "start":
        run("queue", device_id=device)
    original = transport.observe

    def fail_after_effect(seconds):
        dispatched = transport.quits if command == "stop" else transport.plays
        if dispatched:
            journals = [json.loads(path.read_text()) for path in (runtime / "runs").glob("*.json")]
            assert any(
                report["command"] == command and report["commands"][0]["returned"]
                for report in journals
                if report["commands"]
            )
            raise TimeoutError("secret-bearing device payload")
        return original(seconds)

    transport.observe = fail_after_effect
    report, code = run(command, **({"device_id": device} if command == "probe" else {}))
    assert code == 1 and report["state"] == "unconfirmed"
    assert report["commands"][0]["returned"]
    assert "secret-bearing" not in json.dumps(report)
    if command != "probe":
        assert load_queue(runtime)["items"][0]["state"] == "unconfirmed"


def test_injected_store_and_ownership_resolver_support_consecutive_commands(system):
    from tests.playback_support import MemoryStore

    _, transport, device, run = system
    store = MemoryStore()
    run("queue", device_id=device)
    for command in ("start", "status", "stop"):
        report, code = run(command, store=store, ownership=store.current)
        assert code == 0
    assert report["stop_confirmed"] and len(transport.plays) == 1


def test_cast_wire_fields_remain_compatible(system):
    _, _, _, _run = system
    report = started(system)
    assert {"schema_version", "commands", "observations", "state", "evidence"} <= report.keys()
    receiver = next(event for event in report["observations"] if event["kind"] == "receiver")
    media = next(event for event in report["observations"] if event["kind"] == "media")
    assert receiver["app_name"] == "YouTube"
    assert media["media_session_id"] == 1
    assert report["commands"][0]["action"] == "play_video"
    assert {"requested_at", "recorded_at", "returned", "outcome"} <= report["commands"][0].keys()


def test_discovery_error_does_not_expose_transport_payload(tmp_path):
    discovery = Mock(side_effect=RuntimeError("secret=http://private.example/token"))
    report, code = execute("discover", tmp_path, discover=discovery)
    assert code == 1 and report["schema_version"] == 1
    assert "private.example" not in json.dumps(report)


def test_cli_setup_failure_still_returns_versioned_json(tmp_path, monkeypatch, capsys):
    from tellyq import __main__

    unavailable = tmp_path / "not-a-directory"
    unavailable.write_text("blocked")
    monkeypatch.setattr(__main__, "RUNTIME", unavailable)
    assert __main__.main(["status"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["schema_version"] == 1
    assert result["error"]["type"] == "FileExistsError"
