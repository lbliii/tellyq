"""Opt-in local MCP checkpoint for an already-running foreground owner.

Trace reads status only. Start requires --live, uses the real tellyq-mcp stdio
entry point, and sends a guarded stop after its bounded observation window.
"""

from __future__ import annotations

import argparse
import json
import os
import select
import subprocess
import sys
from pathlib import Path
from re import fullmatch
from time import monotonic, sleep
from typing import Literal, cast
from uuid import uuid4

from tellyq.checkpoint import CheckpointJournal
from tellyq.clock import SystemClock
from tellyq.models import IPCResponse, MCPCheckpointSummary, MCPResult
from tellyq.service import QueueSpec, load_manifest
from tellyq.service_checkpoint import selected_queue
from tellyq.state import save

_MAX_FRAME = 2_000_000


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("MCP returned an invalid object.")
    return cast(dict[str, object], value)


def _maybe_object(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


class MCPClient:
    """Bounded JSON-RPC line client for the installed TellyQ stdio entry point."""

    def __init__(self, runtime: Path, executable: Path | None = None) -> None:
        entry = executable or Path(sys.executable).with_name("tellyq-mcp")
        if not entry.is_file():
            raise FileNotFoundError("Install the optional mcp extra before this checkpoint.")
        environment = os.environ.copy()
        environment["TELLYQ_OWNER_RUNTIME"] = str(runtime.resolve())
        self.process = subprocess.Popen(
            [str(entry), "--mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        self._buffer = bytearray()
        self._next_id = 1
        try:
            initialized = self.request(
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "tellyq-checkpoint", "version": "1"},
                },
            )
            if _object(initialized.get("result")).get("serverInfo") is None:
                raise ValueError("MCP initialization failed.")
            self.notify("notifications/initialized")
            listed = self.request("tools/list", {})
            tools = _object(listed.get("result")).get("tools")
            if not isinstance(tools, list) or {
                item.get("name") for item in tools if isinstance(item, dict)
            } != {"start", "status", "stop"}:
                raise ValueError("MCP exposed an unexpected tool set.")
        except Exception:
            self.close()
            raise

    def _write(self, frame: dict[str, object]) -> None:
        if self.process.stdin is None:
            raise RuntimeError("MCP stdin is unavailable.")
        self.process.stdin.write((json.dumps(frame, allow_nan=False) + "\n").encode())
        self.process.stdin.flush()

    def notify(self, method: str) -> None:
        self._write({"jsonrpc": "2.0", "method": method})

    def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        identifier = self._next_id
        self._next_id += 1
        self._write({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})
        deadline = monotonic() + 5
        if self.process.stdout is None:
            raise RuntimeError("MCP stdout is unavailable.")
        while b"\n" not in self._buffer:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("MCP response deadline expired.")
            readable, _, _ = select.select([self.process.stdout], [], [], remaining)
            if not readable:
                raise TimeoutError("MCP response deadline expired.")
            chunk = os.read(self.process.stdout.fileno(), 4096)
            if not chunk:
                raise RuntimeError("MCP process ended before responding.")
            self._buffer.extend(chunk)
            if len(self._buffer) > _MAX_FRAME:
                raise ValueError("MCP response exceeds the frame limit.")
        line, _, remainder = self._buffer.partition(b"\n")
        self._buffer = bytearray(remainder)
        response = _object(json.loads(line))
        if response.get("jsonrpc") != "2.0" or response.get("id") != identifier:
            raise ValueError("MCP returned an unexpected response frame.")
        return response

    def call(self, name: str, *, command_id: str | None = None) -> MCPResult:
        arguments = {"command_id": command_id} if command_id is not None else {}
        frame = self.request("tools/call", {"name": name, "arguments": arguments})
        result = _object(frame.get("result"))
        if result.get("isError") is True:
            raise ValueError("MCP tool dispatch failed.")
        structured = _object(result.get("structuredContent"))
        if structured.get("schema_version") != 1 or not isinstance(structured.get("ok"), bool):
            raise ValueError("MCP tool returned an invalid TellyQ result.")
        return cast(MCPResult, structured)

    def close(self) -> None:
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)
        if self.process.stdout is not None:
            self.process.stdout.close()
        if self.process.stderr is not None:
            self.process.stderr.close()

    def __enter__(self) -> MCPClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _status(client: MCPClient, spec: QueueSpec, journal: CheckpointJournal) -> IPCResponse:
    result = client.call("status")
    journal.write("mcp_status", result=result)
    if not result["ok"] or "response" not in result:
        raise RuntimeError("The selected owner is unavailable through MCP.")
    response = result["response"]
    selected_queue(response, spec)
    return response


def _playback(response: IPCResponse) -> dict[str, object]:
    return (
        _maybe_object(response["snapshot"]["view"].get("playback"))
        if "snapshot" in response
        else {}
    )


def run_checkpoint(
    mode: Literal["trace", "start"],
    runtime: Path,
    spec: QueueSpec,
    journal: CheckpointJournal,
    *,
    code_revision: str,
    seconds: float,
    poll_seconds: float,
    cleanup_seconds: float,
) -> MCPCheckpointSummary:
    """Record MCP command receipts separately from fresh receiver observations."""
    summary: MCPCheckpointSummary = {
        "schema_version": 1,
        "mode": mode,
        "queue_id": spec.queue_id,
        "code_revision": code_revision,
        "start_acknowledged": False,
        "receiver_playback_confirmed": False,
        "stop_acknowledged": False,
        "stop_observed": False,
        "owner_survived_mcp_exit": False,
        "interrupted": False,
        "visual_confirmation": None,
        "live_acceptance": False,
    }
    with MCPClient(runtime) as first:
        baseline = _status(first, spec, journal)
        if mode == "trace":
            summary["owner_survived_mcp_exit"] = None
            return summary
        start_id = str(uuid4())
        summary["start_command_id"] = start_id
        journal.write("mcp_command_intent", action="start", command_id=start_id, outcome="unknown")
        try:
            start = first.call("start", command_id=start_id)
            journal.write("mcp_command_result", action="start", result=start)
            summary["start_acknowledged"] = start["ok"] and start["code"] == "accepted"
            start_ticket = (
                _object(start.get("response")).get("ticket_id") if "response" in start else None
            )
        except (Exception, KeyboardInterrupt) as exc:
            journal.write(
                "mcp_command_error",
                action="start",
                error_type=type(exc).__name__,
                outcome="unknown",
            )
            summary["interrupted"] = isinstance(exc, KeyboardInterrupt)
            start_ticket = None
    # The original MCP process is closed before reopening. The foreground owner
    # must retain the attempt independently of the stdio client's lifetime.
    before_stop: object = None
    try:
        with MCPClient(runtime) as second:
            status = _status(second, spec, journal)
            summary["owner_survived_mcp_exit"] = True
            before_start = _playback(baseline).get("observed_at")
            deadline = monotonic() + seconds
            while not summary["interrupted"] and monotonic() < deadline:
                playback = _playback(status)
                evidence = _maybe_object(playback.get("evidence"))
                receipt = _maybe_object(playback.get("receipt"))
                if (
                    isinstance(start_ticket, str)
                    and _maybe_object(status["snapshot"]["view"].get("queue")).get(
                        "current_item_id"
                    )
                    == spec.items[0].item_id
                    and playback.get("content_id") == spec.items[0].content.content_id
                    and playback.get("ownership_lost") is False
                    and playback.get("observed_at") is not None
                    and playback.get("observed_at") != before_start
                    and receipt.get("request_id") == start_ticket
                    and evidence.get("receiver_playback_confirmed") is True
                ):
                    summary["receiver_playback_confirmed"] = True
                    break
                sleep(poll_seconds)
                status = _status(second, spec, journal)
            before_stop = _playback(status).get("observed_at")
    except (Exception, KeyboardInterrupt) as exc:
        journal.write("mcp_observation_error", error_type=type(exc).__name__)
        summary["interrupted"] = isinstance(exc, KeyboardInterrupt)
    stop_id = str(uuid4())
    summary["stop_command_id"] = stop_id
    with MCPClient(runtime) as cleanup:
        journal.write("mcp_command_intent", action="stop", command_id=stop_id, outcome="unknown")
        try:
            stop = cleanup.call("stop", command_id=stop_id)
            journal.write("mcp_command_result", action="stop", result=stop)
            summary["stop_acknowledged"] = stop["ok"] and stop["code"] == "accepted"
            stop_ticket = (
                _object(stop.get("response")).get("ticket_id") if "response" in stop else None
            )
        except Exception as exc:
            journal.write(
                "mcp_command_error", action="stop", error_type=type(exc).__name__, outcome="unknown"
            )
            stop_ticket = None
        deadline = monotonic() + cleanup_seconds
        while monotonic() < deadline:
            status = _status(cleanup, spec, journal)
            playback = _playback(status)
            evidence = _maybe_object(playback.get("evidence"))
            receipt = _maybe_object(playback.get("receipt"))
            if (
                isinstance(stop_ticket, str)
                and playback.get("state") == "stopped"
                and playback.get("ownership_lost") is False
                and playback.get("observed_at") is not None
                and playback.get("observed_at") != before_stop
                and receipt.get("request_id") == stop_ticket
                and evidence.get("reason") == "stop_observed"
            ):
                summary["stop_observed"] = True
                break
            sleep(poll_seconds)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("trace", "start"))
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--code-revision", required=True, help="Tested Git commit identifier")
    parser.add_argument(
        "--live", action="store_true", help="Explicitly enable start and guarded stop"
    )
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    parser.add_argument("--cleanup-seconds", type=float, default=15)
    args = parser.parse_args(argv)
    if (args.mode == "start") != args.live:
        parser.error("start requires --live; trace does not accept --live")
    if (
        not 1 <= args.seconds <= 120
        or not 0.1 <= args.poll_seconds <= 5
        or not 1 <= args.cleanup_seconds <= 30
    ):
        parser.error("Checkpoint timing is outside bounded ranges")
    try:
        private = Path("runtime").resolve()
        if any(
            not path.resolve().is_relative_to(private)
            for path in (args.runtime, args.manifest, args.output)
        ):
            raise ValueError("Runtime, manifest and output must stay under runtime/.")
        if not args.runtime.is_dir():
            raise ValueError("The selected owner runtime must already exist.")
        if fullmatch(r"[0-9a-fA-F]{7,40}", args.code_revision) is None:
            raise ValueError("Code revision must be a Git commit identifier.")
        spec = load_manifest(args.manifest)
        with CheckpointJournal(args.output, SystemClock()) as journal:
            journal.write(
                "selection",
                mode=args.mode,
                runtime=str(args.runtime.resolve()),
                manifest=str(args.manifest.resolve()),
                queue_id=spec.queue_id,
                code_revision=args.code_revision,
                options={
                    "seconds": args.seconds,
                    "poll_seconds": args.poll_seconds,
                    "cleanup_seconds": args.cleanup_seconds,
                },
            )
            summary = run_checkpoint(
                cast(Literal["trace", "start"], args.mode),
                args.runtime,
                spec,
                journal,
                code_revision=args.code_revision,
                seconds=args.seconds,
                poll_seconds=args.poll_seconds,
                cleanup_seconds=args.cleanup_seconds,
            )
            save(args.output / "summary.json", summary)
        print(json.dumps(summary, allow_nan=False), flush=True)
        if summary["interrupted"]:
            return 130
        return int(
            args.mode == "start"
            and not (
                summary["receiver_playback_confirmed"]
                and summary["stop_observed"]
                and summary["owner_survived_mcp_exit"]
            )
        )
    except (Exception, KeyboardInterrupt) as exc:
        print(
            json.dumps(
                {"error": type(exc).__name__, "visual_confirmation": None, "live_acceptance": False}
            ),
            flush=True,
        )
        return 130 if isinstance(exc, KeyboardInterrupt) else 1


if __name__ == "__main__":
    raise SystemExit(main())
