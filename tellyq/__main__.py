"""Structured commands; JSON goes to stdout and local diagnostics to runtime/."""

import argparse
import json
import logging
from pathlib import Path

from .controller import execute
from .models import ServiceReport
from .service import legacy_spec, load_manifest, run_foreground
from .service_cli import (
    OWNER_COMMANDS,
    OWNER_TOOL_COMMANDS,
    OWNER_TOOL_SPECS,
    has_endpoint,
    owner_command,
    owner_tool_command,
)
from .state import RUNTIME


def _emit_service(report: ServiceReport) -> None:
    print(json.dumps(report), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="TellyQ: verified YouTube playback on one Cast receiver"
    )
    parser.add_argument(
        "--runtime", type=Path, default=None, help="private state directory (default: ./runtime)"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("discover", help="list Cast receivers without starting playback")
    queue = commands.add_parser("queue", help="save the one-item Bob Ross queue without playback")
    queue.add_argument("--device", required=True, help="exact UUID from discover")
    for name in ("probe", "start", "status", "stop", "pause", "resume"):
        spec = next((spec for spec in OWNER_TOOL_SPECS if spec.name == name), None)
        sub = commands.add_parser(name, help=spec.description if spec is not None else None)
        if spec is not None:
            sub.add_argument(
                "--owner",
                action="store_true",
                help="require the foreground owner and emit the shared CLI/MCP result",
            )
        if name != "start":
            sub.add_argument("--device", required=name == "probe", help="exact UUID from discover")
        if name in {"probe", "start"}:
            sub.add_argument(
                "--seconds", type=int, default=None, choices=range(5, 121), metavar="5..120"
            )
        if (spec is not None and spec.accepts_command_id) or name in {"pause", "resume"}:
            sub.add_argument("--command-id", help="stable local owner command ID for retry/query")
    serve = commands.add_parser("serve", help="own one queue in the foreground; start is explicit")
    source = serve.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest", type=Path, help="version 1 multi-item YouTube queue manifest")
    source.add_argument(
        "--import-legacy",
        action="store_true",
        help="explicitly import runtime/queue.json without rewriting it",
    )
    serve.add_argument("--queue-id", help="new durable queue identity for explicit legacy import")
    ticket = commands.add_parser("ticket", help="query a command's local owner ticket")
    ticket.add_argument("--command-id", required=True)
    shutdown = commands.add_parser(
        "shutdown", help="close owner without implicitly stopping the TV"
    )
    shutdown.add_argument("--timeout", type=int, default=0, choices=range(6))
    args = parser.parse_args(argv)
    if args.command == "serve" and bool(args.import_legacy) != bool(args.queue_id):
        parser.error("--queue-id is required only with --import-legacy")
    runtime = args.runtime if args.runtime is not None else RUNTIME
    try:
        if args.command == "serve":
            spec = (
                legacy_spec(runtime, args.queue_id)
                if args.import_legacy
                else load_manifest(args.manifest)
            )
            return run_foreground(runtime, spec, _emit_service)
        if args.command in OWNER_COMMANDS and (
            has_endpoint(runtime)
            or getattr(args, "owner", False)
            or args.command in {"ticket", "shutdown"}
        ):
            if getattr(args, "seconds", None) is not None:
                raise ValueError(
                    "--seconds configures a direct start; foreground commands use ticket/status queries."
                )
            if args.command in OWNER_TOOL_COMMANDS:
                result = owner_tool_command(
                    args.command,
                    runtime,
                    device=getattr(args, "device", None),
                    command_id=getattr(args, "command_id", None),
                )
                response = result if args.owner else result.get("response", result)
            else:
                response = owner_command(
                    args.command,
                    runtime,
                    device=getattr(args, "device", None),
                    command_id=getattr(args, "command_id", None),
                    timeout=getattr(args, "timeout", 0),
                )
            print(json.dumps(response, indent=2), flush=True)
            return 0 if response["ok"] else 1
        if getattr(args, "command_id", None) is not None:
            raise ValueError(
                "--command-id requires a foreground owner; no direct command was sent."
            )
        runtime.mkdir(exist_ok=True, parents=True)
        logging.basicConfig(filename=runtime / "controller.log", level=logging.WARNING)
        report, code = execute(
            args.command,
            runtime,
            getattr(args, "device", None),
            getattr(args, "seconds", None) or 30,
        )
    except Exception as exc:
        report, code = (
            {
                "schema_version": 1,
                "error": {
                    "type": type(exc).__name__,
                    "message": "The command could not complete; inspect local diagnostics.",
                },
            },
            1,
        )
    print(json.dumps(report, indent=2), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
