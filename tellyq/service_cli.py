"""CLI clients of the foreground owner; no device fallback after IPC selection."""

from hashlib import sha256
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from .domain.values import CommandAction, ContentKind, ContentRef, PlaybackRequest, PlaybackTarget
from .models import IPCResponse
from .runner import RunnerCommand
from .runner_ipc import IPCUnavailable, RunnerIPCClient, endpoint_path

OWNER_COMMANDS = frozenset({"start", "status", "stop", "pause", "resume", "ticket", "shutdown"})


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
