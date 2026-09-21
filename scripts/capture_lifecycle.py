"""Explicit opt-in passive lifecycle capture and offline fixture export."""

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID

from tellyq.clock import SystemClock
from tellyq.domain.values import ContentRef, PlaybackTarget
from tellyq.lifecycle import JsonlJournal, capture_lifecycle, export_capture, replay_capture


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser(
        "capture", help="Read-only capture; does not launch or stop playback"
    )
    capture.add_argument("--device", type=UUID, required=True)
    capture.add_argument("--content", required=True, help="Exact YouTube video ID, not a watch URL")
    capture.add_argument(
        "--seconds", type=float, required=True, help="Bounded capture duration, up to 14400 seconds"
    )
    capture.add_argument("--output", type=Path, required=True, help="New JSONL path under runtime/")
    export = commands.add_parser(
        "sanitize", help="Export an allowlisted, pseudonymous fixture offline"
    )
    export.add_argument("source", type=Path)
    export.add_argument("destination", type=Path)
    replay = commands.add_parser(
        "replay", help="Recompute evidence offline from normalized journal"
    )
    replay.add_argument("source", type=Path)
    replay.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "sanitize":
            export_capture(args.source, args.destination)
            print(json.dumps({"exported": True, "review_required": True}))
            return 0
        if args.command == "replay":
            replay_capture(args.source, args.destination)
            print(json.dumps({"replayed": True, "new_hardware_evidence": False}))
            return 0
        if not args.output.resolve().is_relative_to(Path("runtime").resolve()):
            parser.error("Capture output must be under runtime/.")
        if not 0 < args.seconds <= 14_400:
            parser.error("Capture duration must be positive and at most 14400 seconds.")
        if len(args.content) != 11 or any(
            not (char.isascii() and (char.isalnum() or char in "_-")) for char in args.content
        ):
            parser.error("Content must be an exact 11-character YouTube video ID.")
        from tellyq.cast_backend import open_backend

        clock = SystemClock()
        target = PlaybackTarget(str(args.device), "cast")
        with (
            JsonlJournal(args.output) as sink,
            open_backend(target, clock, 2, read_only=True) as (backend, _device),
        ):
            print(json.dumps({"capturing": True, "read_only": True}), file=sys.stderr, flush=True)
            end = capture_lifecycle(
                backend,
                target=target,
                content=ContentRef("youtube", args.content),
                clock=clock,
                sink=sink,
                seconds=args.seconds,
            )
        print(
            json.dumps(
                {
                    key: end[key]
                    for key in (
                        "stop_reason",
                        "attached",
                        "state",
                        "evidence",
                        "entire_run_observed",
                    )
                }
            )
        )
        return (
            130
            if end["stop_reason"] == "interrupted"
            else int(end["stop_reason"] == "backend_error")
        )
    except OSError, ValueError:
        print(
            json.dumps(
                {"error": "Lifecycle operation failed; check paths, record format and arguments."}
            )
        )
        return 1
    except Exception:
        print(json.dumps({"error": "Lifecycle connection failed; no playback command was sent."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
