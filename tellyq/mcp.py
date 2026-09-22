"""Optional Milo stdio MCP surface for the existing foreground owner.

This module deliberately imports Milo only when the optional interface is
started. The playback core and the regular ``tellyq`` command therefore remain
usable without the interface dependency installed.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .service_cli import OWNER_TOOL_SPECS, OwnerToolSpec, owner_tool_command

if TYPE_CHECKING:
    from milo import CLI

_RUNTIME_ENV = "TELLYQ_OWNER_RUNTIME"


def configured_runtime() -> Path:
    """Read the owner runtime selected when the MCP process was started."""
    configured = os.environ.get(_RUNTIME_ENV, "")
    if not configured:
        raise RuntimeError(f"{_RUNTIME_ENV} must name the existing owner runtime.")
    runtime = Path(configured)
    if not runtime.is_absolute():
        raise RuntimeError(f"{_RUNTIME_ENV} must be an absolute path.")
    return runtime


def build_cli(runtime: Path) -> CLI:
    """Build the three-tool Milo CLI around one fixed owner runtime."""
    try:
        from milo import CLI
    except ImportError as exc:
        raise RuntimeError(
            "The optional MCP interface requires the 'mcp' dependency extra."
        ) from exc

    cli = CLI(
        name="tellyq-mcp",
        description="TellyQ controls an existing foreground playback owner over local MCP.",
        version="0.1.0",
    )

    # Milo 0.4.3 misrepresents recursive aliases and nullable values in output
    # schemas. Advertise an object here; owner_tool_command retains typed MCPResult.
    for spec in OWNER_TOOL_SPECS:
        handler = _handler(spec, runtime)
        cli.command(spec.name, description=spec.description, annotations=spec.annotations())(
            handler
        )

    return cli


def _handler(spec: OwnerToolSpec, runtime: Path) -> Callable[..., dict]:
    if spec.accepts_command_id:

        def with_id(command_id: str | None = None) -> dict:
            """Submit a guarded owner command.

            Args:
                command_id: Optional stable ID used to retry or query this request.
            """
            return cast(dict, owner_tool_command(spec.name, runtime, command_id=command_id))

        return with_id

    def without_id() -> dict:
        """Read current owner state without changing playback."""
        return cast(dict, owner_tool_command(spec.name, runtime))

    return without_id


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
