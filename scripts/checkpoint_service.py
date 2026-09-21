"""Opt-in service checkpoint; trace is read-only and start must be explicitly selected."""

import argparse
import json
from pathlib import Path
from re import fullmatch

from tellyq.checkpoint import CheckpointJournal
from tellyq.clock import SystemClock
from tellyq.runner_ipc import RunnerIPCClient
from tellyq.service import load_manifest
from tellyq.service_checkpoint import ServiceCheckpointOptions, run_service_checkpoint
from tellyq.state import save


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("trace", "start"))
    parser.add_argument(
        "--runtime",
        type=Path,
        required=True,
        help="Explicit existing service runtime under runtime/",
    )
    parser.add_argument(
        "--manifest", type=Path, required=True, help="Exact private manifest for this owner"
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New exclusive checkpoint directory under runtime/",
    )
    parser.add_argument("--seconds", type=float, default=600)
    parser.add_argument("--poll-seconds", type=float, default=0.1)
    parser.add_argument(
        "--cleanup-stop",
        action="store_true",
        help="Explicitly send guarded stop on exit from start mode",
    )
    parser.add_argument("--cleanup-seconds", type=float, default=15)
    parser.add_argument(
        "--pause-hold",
        type=float,
        help="Pause after verified first playback, then resume after this hold",
    )
    parser.add_argument(
        "--cancel-after", type=float, help="Submit stop after this many client elapsed seconds"
    )
    parser.add_argument(
        "--code-revision", required=True, help="Operator-recorded tested Git revision"
    )
    args = parser.parse_args(argv)
    try:
        private = Path("runtime").resolve()
        if any(
            not path.resolve().is_relative_to(private)
            for path in (args.runtime, args.manifest, args.output)
        ):
            raise ValueError("Runtime, manifest and output must stay under runtime/.")
        if not args.runtime.is_dir():
            raise ValueError("The selected service runtime must already exist.")
        if fullmatch(r"[0-9a-fA-F]{7,40}", args.code_revision) is None:
            raise ValueError("Code revision must be a Git commit identifier.")
        options = ServiceCheckpointOptions(
            args.action,
            args.seconds,
            args.poll_seconds,
            args.cleanup_stop,
            args.cleanup_seconds,
            args.pause_hold,
            args.cancel_after,
        )
        spec = load_manifest(args.manifest)
        # Keep socket round trips bounded. Poll cadence is separate from RPC timeout.
        client = RunnerIPCClient(args.runtime, timeout=1)
        with CheckpointJournal(args.output, SystemClock()) as journal:
            journal.write(
                "selection",
                runtime=str(args.runtime.resolve()),
                manifest=str(args.manifest.resolve()),
                queue_id=spec.queue_id,
                target=spec.target,
                code_revision=args.code_revision,
                options=options,
            )
            summary = run_service_checkpoint(
                client, spec, options, journal, code_revision=args.code_revision
            )
            save(args.output / "summary.json", summary)
        print(json.dumps(summary, allow_nan=False), flush=True)
        if summary["stop_reason"] == "interrupted":
            return 130
        if args.action == "trace":
            return int(summary["stop_reason"] == "error")
        commands = summary["commands"]
        required = {"start"}
        if options.pause_hold is not None:
            required |= {"pause", "resume"}
        if options.cancel_after is not None:
            required.add("stop")
        verified = {
            entry["action"] for entry in commands if entry["first_verified_state_ms"] is not None
        }
        return int(
            summary["stop_reason"] not in {"queue_finished_and_released", "stop_observed"}
            or not required <= verified
            or (
                options.cleanup_stop
                and summary["cleanup"] not in {"release_observed", "stop_observed"}
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
