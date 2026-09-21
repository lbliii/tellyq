import copy
import json
from dataclasses import replace
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest

from tellyq.domain.queue import QueueEntry, QueueMode
from tellyq.domain.values import ContentRef, PlaybackTarget
from tellyq.models import IPCResponse, IPCValue
from tellyq.service import QueueSpec
from tellyq.service_checkpoint import ServiceCheckpointOptions, run_service_checkpoint

_script_spec = spec_from_file_location(
    "checkpoint_service", Path(__file__).resolve().parents[1] / "scripts" / "checkpoint_service.py"
)
assert _script_spec is not None and _script_spec.loader is not None
checkpoint_service = module_from_spec(_script_spec)
_script_spec.loader.exec_module(checkpoint_service)

SPEC = QueueSpec(
    "private-queue",
    PlaybackTarget("00000000-0000-4000-8000-000000000001", "cast"),
    (QueueEntry("private-item", ContentRef("youtube", "f9F7yDjSdNA")),),
)


class Clock:
    value = 0.0

    def now(self):
        return self.value

    def wait(self, seconds):
        self.value += seconds


class Client:
    """A cached-status client whose observations have independent, controllable delays."""

    def __init__(self, clock, *, verify_after=0.5, ticket_after=0.2, finish=False):
        self.clock, self.verify_after, self.ticket_after = clock, verify_after, ticket_after
        self.finish = finish
        self.calls = []
        self.sent = {}
        self.last = None
        self.attempt_id = "private-attempt"
        self.response: IPCResponse = {
            "schema_version": 1,
            "ok": True,
            "code": "status",
            "snapshot": {
                "phase": "running",
                "owns_device": True,
                "cancellation_requested": False,
                "pending_commands": 0,
                "active_command_id": None,
                "last_command_id": None,
                "failure": None,
                "revision": 1,
                "view": {
                    "playback": None,
                    "queue": {
                        "queue_id": SPEC.queue_id,
                        "target": {
                            "device_id": SPEC.target.device_id,
                            "route": "cast",
                            "name": "Private living room",
                        },
                        "cancellation_requested": False,
                        "items_truncated": False,
                        "current_item_id": "private-item",
                        "items": [
                            {
                                "item_id": "private-item",
                                "provider": "youtube",
                                "content_id": "f9F7yDjSdNA",
                                "kind": "video",
                                "intent": "pending",
                            }
                        ],
                    },
                },
            },
        }

    def status(self):
        self.calls.append("status")
        result = copy.deepcopy(self.response)
        if self.last is not None:
            action, sent = self.last
            elapsed = self.clock.now() - sent
            verified = elapsed >= self.verify_after
            result["snapshot"]["view"]["playback"] = {
                "attempt_id": self.attempt_id,
                "content_id": "f9F7yDjSdNA",
                "state": {
                    "start": "playing",
                    "pause": "paused",
                    "resume": "playing",
                    "stop": "stopped",
                }[action]
                if verified
                else "buffering",
                "ownership_lost": False,
                "observed_at": f"private-observation-{action}-{verified}",
                "release_confirmed": self.finish and verified,
                "evidence": {
                    "receiver_playback_confirmed": verified and action in {"start", "resume"},
                    "identity_confirmed": verified,
                    "reason": "stop_observed" if verified and action == "stop" else "unknown",
                },
                "receipt": {
                    "request_id": action,
                    "action": action,
                    "outcome": "accepted",
                    "recorded_at": f"private-receipt-{action}",
                },
            }
            first_item(result)["intent"] = "finished" if self.finish and verified else "current"
        return result

    def submit(self, command):
        action = command.action.value
        self.calls.append(action)
        if command.request is not None:
            self.attempt_id = command.request.attempt_id
        self.sent[action] = self.clock.now()
        self.last = (action, self.clock.now())
        self.clock.wait(0.025)
        return {"schema_version": 1, "ok": True, "code": "accepted", "ticket_id": action}

    def ticket(self, command_id):
        self.calls.append("ticket")
        return {
            "schema_version": 1,
            "ok": True,
            "code": "ticket",
            "ticket": {
                "state": "handled"
                if self.clock.now() - self.sent[command_id] >= self.ticket_after
                else "running",
            },
        }


def first_item(response):
    queue = response["snapshot"]["view"]["queue"]
    assert queue is not None
    return queue["items"][0]


def run(client, clock, **options):
    return run_service_checkpoint(
        client,
        SPEC,
        ServiceCheckpointOptions(seconds=2, **options),
        Mock(),
        now=clock.now,
        wait=clock.wait,
        code_revision="b4ff7d3",
    )


def test_trace_reads_only_even_if_private_handoff_diagnostics_exist():
    clock = Clock()
    client = Client(clock)
    cast(dict[str, object], client.response["snapshot"]["view"])["handoff"] = {
        "first_veto": "private-address-secret"
    }
    result = run(client, clock)
    assert client.calls and set(client.calls) == {"status"}
    assert result["handoff_diagnostics_available"]
    assert result["commands"] == [] and result["cleanup"] == "not_requested"
    assert result["elapsed_ms"] == 2000
    assert "private" not in json.dumps(result)
    assert not result["live_acceptance"] and result["visual_confirmation"] is None


@pytest.mark.parametrize(
    "options",
    [
        {"cleanup_stop": True},
        {"pause_hold": 1},
        {"cancel_after": 1},
        {"seconds": float("nan")},
        {"poll_seconds": 0},
        {"cleanup_seconds": 31},
    ],
)
def test_invalid_or_effectful_trace_options_refuse(options):
    with pytest.raises(ValueError):
        ServiceCheckpointOptions(**options)


def test_exact_manifest_mismatch_never_submits_even_cleanup():
    clock = Clock()
    client = Client(clock)
    queue = client.response["snapshot"]["view"]["queue"]
    assert queue is not None
    cast(dict[str, IPCValue], queue["target"])["device_id"] = "other-device"
    result = run(client, clock, mode="start", cleanup_stop=True)
    assert result["stop_reason"] == "error"
    assert result["cleanup"] == "not_requested"
    assert client.calls == ["status"]


def test_native_mode_mismatch_refuses_before_start_or_cleanup():
    clock, journal = Clock(), Mock()
    client = Client(clock)
    result = run_service_checkpoint(
        client,
        replace(SPEC, mode=QueueMode.NATIVE),
        ServiceCheckpointOptions(mode="start", seconds=2, cleanup_stop=True),
        journal,
        now=clock.now,
        wait=clock.wait,
    )
    assert result["stop_reason"] == "error"
    assert client.calls == ["status"]


def test_native_completion_collects_before_separate_cleanup_stop():
    clock, journal = Clock(), Mock()

    class NativeClient(Client):
        def status(self):
            response = super().status()
            response["snapshot"]["view"]["queue"]["mode"] = "native"
            playback = response["snapshot"]["view"]["playback"]
            if playback:
                playback["release_confirmed"] = False
            return response

    client = NativeClient(clock, finish=True)
    result = run_service_checkpoint(
        client,
        replace(SPEC, mode=QueueMode.NATIVE),
        ServiceCheckpointOptions(mode="start", seconds=2, cleanup_stop=True),
        journal,
        now=clock.now,
        wait=clock.wait,
    )
    assert result["stop_reason"] == "queue_finished"
    assert result["items"][0]["finished_at_ms"] is not None
    assert result["cleanup"] == "stop_observed"
    assert [x for x in client.calls if x in {"start", "stop"}] == ["start", "stop"]


@pytest.mark.parametrize("reason", ["native_successor_pending", "native_reconciliation_required"])
def test_only_expected_native_transition_allows_temporary_predecessor_ownership_loss(reason):
    clock, journal = Clock(), Mock()

    class NativeClient(Client):
        def status(self):
            response = super().status()
            view = response["snapshot"]["view"]
            view["queue"]["mode"] = "native"
            view["handoff"] = {"reason": reason}
            if view["playback"]:
                view["playback"]["ownership_lost"] = True
            return response

    result = run_service_checkpoint(
        NativeClient(clock),
        replace(SPEC, mode=QueueMode.NATIVE),
        ServiceCheckpointOptions(mode="start", seconds=2),
        journal,
        now=clock.now,
        wait=clock.wait,
    )
    assert result["stop_reason"] == (
        "deadline" if reason == "native_successor_pending" else "ownership_lost"
    )


def test_ack_ticket_and_verified_timing_remain_distinct_without_backdating():
    clock = Clock()
    client = Client(clock, verify_after=1.4, ticket_after=0.7, finish=True)
    result = run(client, clock, mode="start", cleanup_stop=True)
    command = result["commands"][0]
    assert command["acknowledgement_ms"] == 25
    assert command["ticket_completed_ms"] >= 700
    assert command["first_verified_state_ms"] >= 1400
    assert command["ticket_completed_ms"] < command["first_verified_state_ms"]
    assert result["stop_reason"] == "queue_finished_and_released"
    assert result["cleanup"] == "release_observed"
    assert client.calls.count("start") == 1 and "stop" not in client.calls


def test_never_promotes_accepted_handled_ticket_to_verified_effect():
    clock = Clock()
    client = Client(clock, verify_after=100)
    result = run(client, clock, mode="start", cleanup_stop=True, cleanup_seconds=1)
    assert result["stop_reason"] == "deadline"
    assert result["cleanup"] == "unconfirmed"
    assert all(command["first_verified_state_ms"] is None for command in result["commands"])
    assert all(command["ticket_completed_ms"] is not None for command in result["commands"])
    assert client.calls.count("start") == client.calls.count("stop") == 1
    assert result["elapsed_ms"] >= 3000


def test_cancel_in_same_poll_prevents_pause_after_stop():
    clock = Clock()
    client = Client(clock, verify_after=1)
    result = run(client, clock, mode="start", pause_hold=1, cancel_after=1, cleanup_stop=True)
    assert "pause" not in client.calls and "resume" not in client.calls
    assert client.calls.count("stop") == 1
    assert result["cleanup"] == "stop_observed"


def test_uncertain_stop_submission_is_not_retried_during_cleanup(monkeypatch):
    clock = Clock()
    client = Client(clock)
    original = client.submit

    def submit(command):
        result = original(command)
        if command.action.value == "stop":
            raise TimeoutError("private-address-secret")
        return result

    monkeypatch.setattr(client, "submit", submit)
    result = run(client, clock, mode="start", cancel_after=1, cleanup_stop=True)
    assert client.calls.count("stop") == 1
    assert result["cleanup"] == "uncertain_stop_submission"
    assert result["error_type"] == "TimeoutError"
    assert "private" not in json.dumps(result)


def test_pause_hold_starts_after_verified_pause_and_resume_is_observed():
    clock = Clock()
    client = Client(clock, verify_after=0.1)
    result = run(client, clock, mode="start", pause_hold=1)
    actions = [entry["action"] for entry in result["commands"]]
    assert actions == ["start", "pause", "resume"]
    pause, resume = result["commands"][1:]
    assert resume["submitted_ms"] >= pause["submitted_ms"] + pause["first_verified_state_ms"] + 1000
    assert resume["first_verified_state_ms"] is not None


def test_public_api_rejects_arbitrary_revision_before_reading_service():
    client = Mock()
    with pytest.raises(ValueError, match="revision"):
        run_service_checkpoint(
            client, SPEC, ServiceCheckpointOptions(), Mock(), code_revision="private-secret"
        )
    client.status.assert_not_called()


def test_cli_help_performs_no_service_or_device_io(monkeypatch, capsys):
    client = Mock()
    monkeypatch.setattr(checkpoint_service, "RunnerIPCClient", client)
    with pytest.raises(SystemExit) as exited:
        checkpoint_service.main(["--help"])
    assert exited.value.code == 0
    assert "trace" in capsys.readouterr().out
    client.assert_not_called()


def test_script_rejects_paths_outside_private_runtime_before_client(monkeypatch, tmp_path, capsys):
    client = Mock()
    monkeypatch.setattr(checkpoint_service, "RunnerIPCClient", client)
    monkeypatch.chdir(tmp_path)
    assert (
        checkpoint_service.main(
            [
                "start",
                "--runtime",
                str(tmp_path),
                "--manifest",
                "not-private.json",
                "--output",
                "runtime/out",
                "--code-revision",
                "b4ff7d3",
            ]
        )
        == 1
    )
    assert "ValueError" in capsys.readouterr().out
    client.assert_not_called()


def test_new_checkpoint_output_never_overwrites_previous_attempt(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    runtime = Path("runtime/owner")
    runtime.mkdir(parents=True)
    output = Path("runtime/existing-checkpoint")
    output.mkdir()
    (output / "summary.json").write_text("previous evidence")
    monkeypatch.setattr(checkpoint_service, "load_manifest", lambda _: SPEC)
    client = Mock()
    monkeypatch.setattr(checkpoint_service, "RunnerIPCClient", client)
    assert (
        checkpoint_service.main(
            [
                "trace",
                "--runtime",
                str(runtime),
                "--manifest",
                "runtime/manifest.json",
                "--output",
                str(output),
                "--code-revision",
                "b4ff7d3",
                "--seconds",
                "1",
            ]
        )
        == 1
    )
    client.return_value.status.assert_not_called()
    assert (output / "summary.json").read_text() == "previous evidence"
    capsys.readouterr()


def test_repeated_title_handoffs_use_item_identity_and_never_elapsed_duration():
    clock = Clock()
    spec = QueueSpec(
        SPEC.queue_id,
        SPEC.target,
        tuple(QueueEntry(f"item-{index}", SPEC.items[0].content) for index in range(3)),
    )
    client = Client(clock)
    original = client.status

    def status():
        result = original()
        index = min(2, int(clock.now() / 0.5))
        queue = result["snapshot"]["view"]["queue"]
        assert queue is not None
        queue["current_item_id"] = spec.items[index].item_id
        queue["items"] = [
            {
                "item_id": item.item_id,
                "provider": "youtube",
                "content_id": "f9F7yDjSdNA",
                "kind": "video",
                "intent": "finished" if i < index else "current" if i == index else "pending",
            }
            for i, item in enumerate(spec.items)
        ]
        result["snapshot"]["view"]["playback"] = {
            "content_id": "f9F7yDjSdNA",
            "attempt_id": f"attempt-{index}",
            "ownership_lost": False,
            "duration": 0.001,
            "evidence": {"receiver_playback_confirmed": True},
        }
        return result

    client.status = Mock(side_effect=status)
    result = run_service_checkpoint(
        client, spec, ServiceCheckpointOptions(seconds=2), Mock(), now=clock.now, wait=clock.wait
    )
    assert result["verified_handoffs"] == 2
    assert all(item["distinct_attempts_observed"] == 1 for item in result["items"])
    assert result["items"][2]["finished_at_ms"] is None  # Runtime never fabricates completion.
    assert set(client.calls) == {"status"}


def test_cached_previous_state_does_not_verify_new_command(monkeypatch):
    clock = Clock()
    client = Client(clock, verify_after=0)
    original = client.status

    def status():
        result = original()
        if client.last:
            playback = result["snapshot"]["view"]["playback"]
            assert playback is not None
            playback["receipt"] = {"action": "pause", "outcome": "accepted", "recorded_at": "old"}
        return result

    monkeypatch.setattr(client, "status", status)
    result = run(client, clock, mode="start")
    assert result["commands"][0]["first_verified_state_ms"] is None


def test_later_attempt_cannot_supply_start_command_timing(monkeypatch):
    clock = Clock()
    client = Client(clock, verify_after=0)
    original = client.status

    def status():
        result = original()
        if client.last:
            playback = result["snapshot"]["view"]["playback"]
            assert playback is not None
            playback["attempt_id"] = "different-successor-attempt"
        return result

    monkeypatch.setattr(client, "status", status)
    result = run(client, clock, mode="start")
    assert result["commands"][0]["first_verified_state_ms"] is None


@pytest.mark.parametrize("ticket_state", ["canceled", "failed"])
def test_failed_or_canceled_ticket_cannot_verify_accepted_receipt(monkeypatch, ticket_state):
    clock = Clock()
    client = Client(clock, verify_after=0)
    original = client.ticket

    def ticket(command_id):
        result = original(command_id)
        result["ticket"]["state"] = ticket_state
        return result

    monkeypatch.setattr(client, "ticket", ticket)
    result = run(client, clock, mode="start")
    assert result["commands"][0]["ticket_state"] == ticket_state
    assert result["commands"][0]["first_verified_state_ms"] is None


def test_other_clients_same_action_same_attempt_does_not_verify_our_ticket(monkeypatch):
    clock = Clock()
    client = Client(clock, verify_after=0)
    original = client.status

    def status():
        result = original()
        if client.last:
            playback = result["snapshot"]["view"]["playback"]
            assert playback is not None
            receipt = cast(dict[str, IPCValue], playback["receipt"])
            receipt["request_id"] = "another-clients-start"
        return result

    monkeypatch.setattr(client, "status", status)
    result = run(client, clock, mode="start")
    assert result["commands"][0]["ticket_state"] == "handled"
    assert result["commands"][0]["first_verified_state_ms"] is None
