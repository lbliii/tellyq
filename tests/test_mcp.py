"""Offline stdio MCP checks against a fake private foreground-owner socket."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path
from threading import Event, Thread
from typing import Any
from unittest.mock import Mock

import pytest

import tellyq.mcp as mcp
from tellyq import service_cli
from tellyq.runner_ipc import IPCUnavailable

pytest.importorskip("milo")

_REAL_SOCKET = socket.socket
_DEVICE = "00000000-0000-4000-8000-000000000001"


def _unix_socket(family: int = socket.AF_INET, *args: Any, **kwargs: Any) -> socket.socket:
    if family != socket.AF_UNIX:
        raise AssertionError("Only AF_UNIX is allowed in the MCP harness.")
    return _REAL_SOCKET(family, *args, **kwargs)


_CHILD_BOOTSTRAP = """
import runpy
import socket

real_socket = socket.socket
def unix_only_socket(family=socket.AF_INET, *args, **kwargs):
    if family != socket.AF_UNIX:
        raise AssertionError('Only AF_UNIX is allowed in the MCP subprocess.')
    return real_socket(family, *args, **kwargs)
socket.socket = unix_only_socket
socket.getaddrinfo = lambda *args, **kwargs: (_ for _ in ()).throw(
    AssertionError('DNS is forbidden in the MCP subprocess.')
)
runpy.run_module('tellyq.mcp', run_name='__main__')
"""


class FakeOwner:
    def __init__(self, runtime: Path) -> None:
        self.runtime = runtime
        runtime.mkdir(mode=0o700)
        self.endpoint = runtime / "runner.sock"
        self.endpoint.touch(mode=0o600)
        self.endpoint.unlink()
        self.listener = _REAL_SOCKET(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.endpoint))
        self.endpoint.chmod(0o600)
        self.listener.listen(8)
        self.closed = Event()
        self.requests: list[dict[str, Any]] = []
        self.thread = Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self.closed.is_set():
            self.listener.settimeout(0.2)
            try:
                connection, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if self.closed.is_set():
                    return
                raise
            with connection:
                frame = b""
                while not frame.endswith(b"\n"):
                    part = connection.recv(4096)
                    if not part:
                        break
                    frame += part
                if not frame.endswith(b"\n"):
                    continue
                request = json.loads(frame)
                self.requests.append(request)
                connection.sendall((json.dumps(self._response(request)) + "\n").encode())

    def _response(self, request: dict[str, Any]) -> dict[str, Any]:
        operation = request["operation"]
        if operation == "status":
            return {
                "schema_version": 1,
                "ok": True,
                "code": "status",
                "snapshot": {
                    "phase": "running",
                    "revision": 1,
                    "owns_device": True,
                    "cancellation_requested": False,
                    "pending_commands": 0,
                    "active_command_id": None,
                    "last_command_id": None,
                    "view": {
                        "playback": None,
                        "queue": {
                            "queue_id": "queue-a",
                            "target": {"device_id": _DEVICE, "route": "cast", "name": None},
                            "items": [
                                {
                                    "item_id": "item-a",
                                    "provider": "youtube",
                                    "content_id": "video-a",
                                    "kind": "video",
                                }
                            ],
                        },
                    },
                    "failure": None,
                },
            }
        command = request["command"]
        return {
            "schema_version": 1,
            "ok": True,
            "code": "accepted",
            "ticket_id": command["command_id"],
            "ticket": {
                "command_id": command["command_id"],
                "action": command["action"],
                "state": "queued",
                "failure": None,
                "view": None,
            },
        }

    def close(self) -> None:
        self.closed.set()
        self.listener.close()
        self.thread.join(2)
        if self.endpoint.exists():
            self.endpoint.unlink()


def _process(runtime: Path) -> subprocess.Popen[str]:
    environment = os.environ.copy()
    environment["TELLYQ_OWNER_RUNTIME"] = str(runtime)
    environment["PYTHONPATH"] = str(Path(__file__).parents[1])
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD_BOOTSTRAP, "--mcp"],
        cwd=Path(__file__).parents[1],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _request(process: subprocess.Popen[str], request: dict[str, Any]) -> dict[str, Any]:
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(json.dumps(request) + "\n")
    process.stdin.flush()
    line = process.stdout.readline()
    assert line, process.stderr.read() if process.stderr is not None else ""
    return json.loads(line)


def _notify(process: subprocess.Popen[str], notification: dict[str, Any]) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(notification) + "\n")
    process.stdin.flush()


def test_stdio_handshake_tools_and_owner_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del tmp_path
    monkeypatch.setattr(socket, "socket", _unix_socket)
    directory = tempfile.TemporaryDirectory(prefix="tq-mcp-", dir="/tmp")
    owner = FakeOwner(Path(directory.name) / "owner")
    process = _process(owner.runtime)
    try:
        initialized = _request(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "clientInfo": {"name": "offline-test", "version": "1"},
                    "capabilities": {},
                },
            },
        )
        assert initialized["result"]["serverInfo"]["name"] == "tellyq-mcp"
        _notify(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = _request(
            process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        tools = listed["result"]["tools"]
        assert {tool["name"] for tool in tools} == {"start", "status", "stop"}
        assert (
            next(tool for tool in tools if tool["name"] == "status")["annotations"]["readOnlyHint"]
            is True
        )

        status = _request(
            process,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "status", "arguments": {}},
            },
        )
        assert status["result"]["structuredContent"]["response"]["code"] == "status"
        from tellyq.service_cli import owner_command

        assert status["result"]["structuredContent"]["response"] == owner_command(
            "status", owner.runtime
        )
        started = _request(
            process,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "start", "arguments": {"command_id": "mcp-start"}},
            },
        )
        assert started["result"]["structuredContent"]["response"]["ticket_id"] == "mcp-start"
        retried = _request(
            process,
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {"name": "start", "arguments": {"command_id": "mcp-start"}},
            },
        )
        assert retried["result"]["structuredContent"] == started["result"]["structuredContent"]
        stopped = _request(
            process,
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "stop", "arguments": {"command_id": "mcp-stop"}},
            },
        )
        assert stopped["result"]["structuredContent"]["response"]["ticket_id"] == "mcp-stop"
        invalid = _request(
            process,
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "status", "arguments": {"unexpected": True}},
            },
        )
        assert invalid["result"]["isError"] is True
        invalid_id = _request(
            process,
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {"name": "start", "arguments": {"command_id": "bad id"}},
            },
        )
        assert invalid_id["result"]["structuredContent"]["code"] == "invalid_command_id"
        unknown = _request(
            process,
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "pause", "arguments": {}},
            },
        )
        assert unknown["result"]["isError"] is True
    finally:
        assert process.stdin is not None
        process.stdin.close()
        process.wait(timeout=3)
        assert process.stdout is not None and process.stderr is not None
        assert process.stdout.read() == ""
        process.stdout.close()
        process.stderr.close()
        from tellyq.service_cli import owner_command

        assert owner_command("status", owner.runtime)["ok"] is True
        owner.close()
        directory.cleanup()
    assert process.returncode == 0
    assert [request["operation"] for request in owner.requests[:4]] == [
        "status",
        "status",
        "status",
        "submit",
    ]


def test_mcp_exit_leaves_owner_endpoint_and_no_owner_is_machine_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del tmp_path
    monkeypatch.setattr(socket, "socket", _unix_socket)
    directory = tempfile.TemporaryDirectory(prefix="tq-mcp-", dir="/tmp")
    owner = FakeOwner(Path(directory.name) / "owner")
    missing_runtime = Path(directory.name) / "missing-owner"
    process = _process(owner.runtime)
    try:
        _request(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "clientInfo": {"name": "offline-test", "version": "1"},
                    "capabilities": {},
                },
            },
        )
        _notify(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    finally:
        assert process.stdin is not None
        process.stdin.close()
        process.wait(timeout=3)
        assert process.stdout is not None and process.stderr is not None
        process.stdout.close()
        process.stderr.close()
    assert owner.endpoint.exists()
    owner.close()

    process = _process(missing_runtime)
    try:
        _request(process, {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}})
        result = _request(
            process,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "status", "arguments": {}},
            },
        )
        status = result["result"]["structuredContent"]
        start = _request(
            process,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "start", "arguments": {"command_id": "no-owner-start"}},
            },
        )["result"]["structuredContent"]
        stop = _request(
            process,
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "stop", "arguments": {"command_id": "no-owner-stop"}},
            },
        )["result"]["structuredContent"]
        for response in (status, start, stop):
            assert response["ok"] is False
            assert response["code"] == "owner_unavailable"
            assert "unknown" in response["error"]["message"]
    finally:
        assert process.stdin is not None
        process.stdin.close()
        process.wait(timeout=3)
        assert process.stdout is not None and process.stderr is not None
        process.stdout.close()
        process.stderr.close()
        directory.cleanup()


def test_invalid_command_ids_are_bounded_before_owner_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = Mock(side_effect=AssertionError("owner must not be called"))
    monkeypatch.setattr(service_cli, "owner_command", owner)
    for command_id in ("bad id", "a" * 129, ""):
        result = service_cli.owner_tool_command("start", tmp_path, command_id=command_id)
        assert result["code"] == "invalid_command_id"
    owner.assert_not_called()


def test_owner_exception_material_is_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "/private/owner/runtime/session.sock bearer-token"
    for exception in (IPCUnavailable(secret), ValueError(secret)):
        owner = Mock(side_effect=exception)
        monkeypatch.setattr(service_cli, "owner_command", owner)
        result = service_cli.owner_tool_command("status", tmp_path)
        assert secret not in json.dumps(result)
        assert result["ok"] is False


def test_startup_runtime_requires_absolute_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELLYQ_OWNER_RUNTIME", raising=False)
    with pytest.raises(RuntimeError, match="must name"):
        mcp.configured_runtime()
    monkeypatch.setenv("TELLYQ_OWNER_RUNTIME", "relative/runtime")
    with pytest.raises(RuntimeError, match="absolute"):
        mcp.configured_runtime()


def test_mcp_exposes_exact_tool_schemas(tmp_path: Path) -> None:
    from milo.mcp import _list_tools

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        listed = _list_tools(mcp.build_cli(tmp_path), include_ui=False)
    assert not caught
    tools = {tool["name"]: tool for tool in listed}
    assert set(tools) == {"start", "status", "stop"}
    command_id = {
        "type": "string",
        "description": "Optional stable ID used to retry or query this request.",
        "default": None,
    }
    assert tools["start"]["inputSchema"] == {
        "type": "object",
        "properties": {"command_id": command_id},
    }
    assert tools["stop"]["inputSchema"] == {
        "type": "object",
        "properties": {"command_id": command_id},
    }
    assert tools["status"]["inputSchema"] == {"type": "object", "properties": {}}
    assert all(tool["outputSchema"] == {"type": "object"} for tool in tools.values())
    for spec in service_cli.OWNER_TOOL_SPECS:
        tool = tools[spec.name]
        assert tool["description"] == spec.description
        assert tool["annotations"] == spec.annotations()
        assert ("command_id" in tool["inputSchema"]["properties"]) == spec.accepts_command_id
