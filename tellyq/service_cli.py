"""CLI clients of the foreground owner; no device fallback after IPC selection."""

import re
from hashlib import sha256
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from .domain.values import CommandAction, ContentKind, ContentRef, PlaybackRequest, PlaybackTarget
from .models import IPCResponse, MCPResult
from .runner import RunnerCommand
from .runner_ipc import IPCUnavailable, RunnerIPCClient, endpoint_path

OWNER_COMMANDS = frozenset({"start", "status", "stop", "pause", "resume", "ticket", "shutdown"})
OWNER_TOOL_COMMANDS = frozenset({"start", "status", "stop"})
_COMMAND_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_COMMAND_ID_MESSAGE = "command_id must be 1-128 characters: letters, digits, . _ : or -."


def owner_tool_command(
    command: str, runtime: Path, *, command_id: str | None = None, device: str | None = None
) -> MCPResult:
    """Dispatch the common CLI/Python/MCP owner tools with safe structured errors.

    A returned ticket acknowledges the request; it never proves receiver playback.
    The owner retains default ID generation and retry semantics.
    """
    if command not in OWNER_TOOL_COMMANDS:
        return _tool_error("invalid_command", "Only start, status and stop are supported.")
    if command == "status" and command_id is not None:
        return _tool_error("invalid_command_id", "status does not accept command_id.")
    if command_id is not None and (
        not isinstance(command_id, str) or _COMMAND_ID.fullmatch(command_id) is None
    ):
        return _tool_error("invalid_command_id", _COMMAND_ID_MESSAGE)
    try:
        response = owner_command(command, runtime, command_id=command_id, device=device)
    except IPCUnavailable:
        return _tool_error(
            "owner_unavailable",
            "The owner request could not be confirmed; its outcome may be unknown. "
            "Check status before retrying.",
        )
    except ValueError:
        return _tool_error("owner_rejected", "The foreground owner rejected the command.")
    except Exception:
        return _tool_error("owner_request_failed", "The foreground owner request failed.")
    return {
        "schema_version": 1,
        "ok": response["ok"],
        "code": response["code"],
        "response": response,
    }


def _tool_error(code: str, message: str) -> MCPResult:
    return {
        "schema_version": 1,
        "ok": False,
        "code": code,
        "error": {"type": code, "message": message},
    }


def has_endpoint(runtime: Path) -> bool:
    path = endpoint_path(runtime)
    return path.exists() or path.is_symlink()


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise IPCUnavailable("The owner has not published a usable queue snapshot.")
    return cast(dict[str, object], value)


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IPCUnavailable("Owner identity is unavailable.")
    return value


def _queue(response: IPCResponse) -> dict[str, object]:
    if not response["ok"]:
        raise IPCUnavailable("The owner could not return its current state.")
    return _object(_object(_object(response.get("snapshot")).get("view")).get("queue"))


def _target(queue: dict[str, object]) -> PlaybackTarget:
    target = _object(queue.get("target"))
    return PlaybackTarget(_text(target.get("device_id")), _text(target.get("route")))


def start_command(queue: dict[str, object], command_id: str | None) -> RunnerCommand:
    """Start means the first manifest item; subsequent selection belongs to the task.

    Stable default IDs make a repeated start the same intent even after the queue
    advanced. This never turns a status-selected later item into a second launch.
    """
    items = queue.get("items")
    if not isinstance(items, list) or not items:
        raise IPCUnavailable("The owner has not published its first queue item.")
    first = _object(items[0])
    identity = command_id or "start-" + sha256(_text(queue.get("queue_id")).encode()).hexdigest()
    attempt = "attempt-" + sha256(identity.encode()).hexdigest()
    content = ContentRef(
        _text(first.get("provider")),
        _text(first.get("content_id")),
        ContentKind(_text(first.get("kind"))),
    )
    return RunnerCommand(
        identity,
        CommandAction.START,
        PlaybackRequest(
            identity,
            attempt,
            _text(first.get("item_id")),
            content,
            _target(queue),
        ),
    )


def owner_command(
    command: str,
    runtime: Path,
    *,
    device: str | None = None,
    command_id: str | None = None,
    timeout: float = 0,
) -> IPCResponse:
    client = RunnerIPCClient(runtime)
    if command == "ticket":
        if command_id is None:
            raise ValueError("Ticket lookup requires a command ID.")
        return client.ticket(command_id)
    if command == "shutdown":
        return client.shutdown(timeout)
    status = client.status() if command in {"start", "status"} or device is not None else None
    if device is not None:
        assert status is not None
        if str(UUID(device)) != _target(_queue(status)).device_id:
            raise ValueError("The requested receiver differs from the foreground owner's target.")
    if command == "status":
        assert status is not None
        return status
    if command == "start":
        assert status is not None
        request = start_command(_queue(status), command_id)
    elif command in {"stop", "pause", "resume"}:
        request = RunnerCommand(command_id or str(uuid4()), CommandAction(command))
    else:
        raise ValueError("Unsupported owner command.")
    return client.submit(request)
