"""Optional Milo stdio MCP surface for the existing foreground owner.

This module deliberately imports Milo only when the optional interface is
started. The playback core and the regular ``tellyq`` command therefore remain
usable without the interface dependency installed.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from .models import MCPResult
from .runner_ipc import IPCUnavailable
from .service_cli import owner_command

if TYPE_CHECKING:
    from milo import CLI

_RUNTIME_ENV = "TELLYQ_OWNER_RUNTIME"
_COMMAND_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def configured_runtime() -> Path:
    """Read the owner runtime selected when the MCP process was started."""
    configured = os.environ.get(_RUNTIME_ENV, "")
    if not configured:
        raise RuntimeError(f"{_RUNTIME_ENV} must name the existing owner runtime.")
    runtime = Path(configured)
    if not runtime.is_absolute():
        raise RuntimeError(f"{_RUNTIME_ENV} must be an absolute path.")
    return runtime


def _validate_command_id(command_id: str | None) -> str | None:
    if command_id is not None and _COMMAND_ID.fullmatch(command_id) is None:
        raise ValueError("command_id must be 1-128 characters: letters, digits, . _ : or -.")
    return command_id


def _safe_error(exc: Exception) -> MCPResult:
    if isinstance(exc, IPCUnavailable):
        error_type, message = "owner_unavailable", "The configured foreground owner is unavailable."
    elif isinstance(exc, ValueError):
        error_type, message = "owner_rejected", "The foreground owner rejected the command."
    else:
        error_type, message = "owner_request_failed", "The foreground owner request failed."
    return {
        "schema_version": 1,
        "ok": False,
        "code": error_type,
        "error": {"type": error_type, "message": message},
    }


def _call(command: str, runtime: Path, *, command_id: str | None = None) -> MCPResult:
    try:
        validated_id = _validate_command_id(command_id)
    except ValueError:
        return {
            "schema_version": 1,
            "ok": False,
            "code": "invalid_command_id",
            "error": {
                "type": "invalid_command_id",
                "message": "command_id must be 1-128 characters: letters, digits, . _ : or -.",
            },
        }
    try:
        response = owner_command(command, runtime, command_id=validated_id)
    except (IPCUnavailable, ValueError, OSError) as exc:
        return _safe_error(exc)
    except Exception as exc:
        return _safe_error(exc)
    if not response["ok"]:
        return {
            "schema_version": 1,
            "ok": False,
            "code": response["code"],
            "response": response,
        }
    return {
        "schema_version": 1,
        "ok": True,
        "code": response["code"],
        "response": response,
    }


def build_cli(runtime: Path) -> CLI:
    """Build the three-tool Milo CLI around one fixed owner runtime."""
    try:
        from milo import CLI
    except ImportError as exc:  # pragma: no cover - exercised by packaging smoke
        raise RuntimeError(
            "The optional MCP interface requires the 'mcp' dependency extra."
        ) from exc

    cli = CLI(
        name="tellyq-mcp",
        description="TellyQ controls an existing foreground playback owner over local MCP.",
        version="0.1.0",
    )

    @cli.command(
        "start",
        description="Start the owner's first queued item and return its request ticket.",
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    )
    def start(command_id: str | None = None) -> MCPResult:
        """Start the first queued item through the existing foreground owner.

        Args:
            command_id: Optional stable ID used to retry or query this request.
        """
        return _call("start", runtime, command_id=command_id)

    @cli.command(
        "status",
        description="Read the owner's latest timestamped playback evidence.",
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    def status() -> MCPResult:
        """Read current owner state without changing playback."""
        return _call("status", runtime)

    @cli.command(
        "stop",
        description="Request a guarded stop through the existing foreground owner.",
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    def stop(command_id: str | None = None) -> MCPResult:
        """Stop only the session currently owned by the foreground owner.

        Args:
            command_id: Optional stable ID used to retry or query this request.
        """
        return _call("stop", runtime, command_id=command_id)

    return cli


def main(argv: list[str] | None = None) -> int:
    """Run Milo's stdio server using startup-selected owner configuration."""
    try:
        cli = build_cli(configured_runtime())
    except RuntimeError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    cli.run(argv)
    return 0


__all__ = ["build_cli", "configured_runtime", "main"]


if __name__ == "__main__":  # pragma: no cover - exercised by subprocess tests
    raise SystemExit(main())
