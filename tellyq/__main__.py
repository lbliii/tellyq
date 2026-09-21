"""Structured commands; JSON goes to stdout and local diagnostics to runtime/."""

import argparse
import json
import logging

from .controller import execute
from .state import RUNTIME


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="TellyQ: one real YouTube program on one Cast receiver"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("discover", help="list Cast receivers without starting playback")
    queue = commands.add_parser("queue", help="save the one-item Bob Ross queue without playback")
    queue.add_argument("--device", required=True, help="exact UUID from discover")
    for name in ("probe", "start", "status", "stop"):
        sub = commands.add_parser(name)
        if name != "start":
            sub.add_argument("--device", required=name == "probe", help="exact UUID from discover")
        if name in {"probe", "start"}:
            sub.add_argument(
                "--seconds", type=int, default=30, choices=range(5, 121), metavar="5..120"
            )
    args = parser.parse_args(argv)
    try:
        RUNTIME.mkdir(exist_ok=True)
        logging.basicConfig(filename=RUNTIME / "controller.log", level=logging.WARNING)
        report, code = execute(
            args.command, RUNTIME, getattr(args, "device", None), getattr(args, "seconds", 30)
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
