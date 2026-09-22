"""Offline checkpoint runner against the real stdio surface and a fake Unix owner."""

from __future__ import annotations

import socket
import tempfile
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from tellyq.domain.queue import QueueEntry
from tellyq.domain.values import ContentRef, PlaybackTarget
from tellyq.service import QueueSpec
from tests.test_mcp import FakeOwner, _unix_socket

pytest.importorskip("milo")

_path = Path(__file__).resolve().parents[1] / "scripts" / "checkpoint_mcp.py"
_spec = spec_from_file_location("checkpoint_mcp", _path)
assert _spec is not None and _spec.loader is not None
checkpoint_mcp = module_from_spec(_spec)
_spec.loader.exec_module(checkpoint_mcp)

SPEC = QueueSpec(
    "queue-a",
    PlaybackTarget("00000000-0000-4000-8000-000000000001", "cast"),
    (QueueEntry("item-a", ContentRef("youtube", "video-a")),),
)


class Journal:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def write(self, kind: str, **fields: object) -> None:
        self.events.append((kind, fields))


class EvidenceOwner(FakeOwner):
    """Synthetic correlated observations, never a receiver connection."""

    def _response(self, request):
        response = super()._response(request)
        if request["operation"] != "status":
            return response
        command = next(
            (item["command"] for item in reversed(self.requests) if item["operation"] == "submit"),
            None,
        )
        if command is None:
            return response
        action = command["action"]
        response["snapshot"]["view"]["playback"] = {
            "attempt_id": "attempt-a",
            "content_id": "video-a",
            "state": "playing" if action == "start" else "stopped",
            "ownership_lost": False,
            "observed_at": f"after-{action}",
            "receipt": {
                "request_id": command["command_id"],
                "action": action,
                "outcome": "accepted",
                "recorded_at": f"receipt-{action}",
            },
            "evidence": {
                "receiver_playback_confirmed": action == "start",
                "reason": "stop_observed" if action == "stop" else "playing_observed",
            },
        }
        return response


def test_trace_uses_real_stdio_without_owner_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "socket", _unix_socket)
    with tempfile.TemporaryDirectory(prefix="tq-mcp-checkpoint-", dir="/tmp") as directory:
        owner = FakeOwner(Path(directory) / "owner")
        try:
            journal = Journal()
            summary = checkpoint_mcp.run_checkpoint(
                "trace",
                owner.runtime,
                SPEC,
                journal,
                code_revision="abcdef1",
                seconds=1,
                poll_seconds=0.1,
                cleanup_seconds=1,
            )
            assert summary["mode"] == "trace"
            assert summary["live_acceptance"] is False
            assert [request["operation"] for request in owner.requests] == ["status"]
            assert owner.endpoint.exists()
        finally:
            owner.close()


def test_start_reopens_mcp_and_attempts_stop_without_fabricated_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "socket", _unix_socket)
    with tempfile.TemporaryDirectory(prefix="tq-mcp-checkpoint-", dir="/tmp") as directory:
        owner = FakeOwner(Path(directory) / "owner")
        try:
            journal = Journal()
            summary = checkpoint_mcp.run_checkpoint(
                "start",
                owner.runtime,
                SPEC,
                journal,
                code_revision="abcdef1",
                seconds=1,
                poll_seconds=0.1,
                cleanup_seconds=1,
            )
            assert summary["start_acknowledged"] is True
            assert summary["owner_survived_mcp_exit"] is True
            assert summary["stop_acknowledged"] is True
            assert summary["receiver_playback_confirmed"] is False
            assert summary["stop_observed"] is False
            assert summary["visual_confirmation"] is None
            assert summary["live_acceptance"] is False
            submitted = [
                request["command"] for request in owner.requests if request["operation"] == "submit"
            ]
            assert [command["action"] for command in submitted] == ["start", "stop"]
            assert any(
                kind == "mcp_command_intent" and fields["action"] == "stop"
                for kind, fields in journal.events
            )
        finally:
            owner.close()


def test_correlated_synthetic_evidence_satisfies_machine_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "socket", _unix_socket)
    with tempfile.TemporaryDirectory(prefix="tq-mcp-checkpoint-", dir="/tmp") as directory:
        owner = EvidenceOwner(Path(directory) / "owner")
        try:
            summary = checkpoint_mcp.run_checkpoint(
                "start",
                owner.runtime,
                SPEC,
                Journal(),
                code_revision="abcdef1",
                seconds=1,
                poll_seconds=0.1,
                cleanup_seconds=1,
            )
            assert summary["receiver_playback_confirmed"] is True
            assert summary["stop_observed"] is True
            assert summary["owner_survived_mcp_exit"] is True
            assert summary["visual_confirmation"] is None
            assert summary["live_acceptance"] is False
        finally:
            owner.close()


def test_manifest_mismatch_refuses_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "socket", _unix_socket)
    wrong = QueueSpec(
        "another-queue",
        SPEC.target,
        SPEC.items,
    )
    with tempfile.TemporaryDirectory(prefix="tq-mcp-checkpoint-", dir="/tmp") as directory:
        owner = FakeOwner(Path(directory) / "owner")
        try:
            with pytest.raises(ValueError, match="does not match"):
                checkpoint_mcp.run_checkpoint(
                    "start",
                    owner.runtime,
                    wrong,
                    Journal(),
                    code_revision="abcdef1",
                    seconds=1,
                    poll_seconds=0.1,
                    cleanup_seconds=1,
                )
            assert [request["operation"] for request in owner.requests] == ["status"]
        finally:
            owner.close()


@pytest.mark.parametrize("failure", [TimeoutError, KeyboardInterrupt])
def test_uncertain_start_still_attempts_guarded_stop(
    monkeypatch: pytest.MonkeyPatch, failure: type[BaseException]
) -> None:
    monkeypatch.setattr(socket, "socket", _unix_socket)
    original = checkpoint_mcp.MCPClient.call

    def uncertain(client, name, *, command_id=None):
        if name == "start":
            raise failure("uncertain start response")
        return original(client, name, command_id=command_id)

    monkeypatch.setattr(checkpoint_mcp.MCPClient, "call", uncertain)
    with tempfile.TemporaryDirectory(prefix="tq-mcp-checkpoint-", dir="/tmp") as directory:
        owner = FakeOwner(Path(directory) / "owner")
        try:
            journal = Journal()
            summary = checkpoint_mcp.run_checkpoint(
                "start",
                owner.runtime,
                SPEC,
                journal,
                code_revision="abcdef1",
                seconds=1,
                poll_seconds=0.1,
                cleanup_seconds=1,
            )
            assert summary["start_acknowledged"] is False
            assert summary["interrupted"] is (failure is KeyboardInterrupt)
            assert summary["receiver_playback_confirmed"] is False
            assert summary["stop_acknowledged"] is True
            assert any(
                kind == "mcp_command_error" and fields["action"] == "start"
                for kind, fields in journal.events
            )
            submitted = [
                request["command"] for request in owner.requests if request["operation"] == "submit"
            ]
            assert [command["action"] for command in submitted] == ["stop"]
        finally:
            owner.close()


def test_start_requires_explicit_live_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        checkpoint_mcp.main(
            [
                "start",
                "--runtime",
                "runtime/owner",
                "--manifest",
                "runtime/manifest.json",
                "--output",
                "runtime/checkpoint",
                "--code-revision",
                "abcdef1",
            ]
        )
    assert error.value.code == 2
    assert "requires --live" in capsys.readouterr().err
