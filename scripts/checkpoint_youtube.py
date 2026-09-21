"""Explicit one-video live checkpoint; detailed evidence stays in a new private runtime directory."""

import argparse
import json
import sys
from dataclasses import asdict
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from platform import python_version
from re import fullmatch
from uuid import UUID, uuid4

from tellyq.checkpoint import (
    CheckpointJournal,
    CheckpointOptions,
    CheckpointSummary,
    run_checkpoint,
)
from tellyq.clock import SystemClock
from tellyq.domain.values import ContentRef, PlaybackRequest, PlaybackTarget
from tellyq.session_store import JsonSessionStore
from tellyq.state import command_lock, read, save


def _hashes(runtime: Path) -> dict[str, str | None]:
    return {
        name: sha256(path.read_bytes()).hexdigest() if path.exists() else None
        for name in ("queue.json", "session.json")
        for path in (runtime / name,)
    }


def _phase(phase: str) -> None:
    print(json.dumps({"phase": phase}), file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start",), help="Explicit permission for one launch")
    parser.add_argument(
        "--device-config", type=Path, required=True, help="Private JSON with device_id"
    )
    parser.add_argument("--content", required=True, help="Exact 11-character YouTube video ID")
    parser.add_argument(
        "--output", type=Path, required=True, help="New exclusive directory under runtime/"
    )
    parser.add_argument(
        "--seconds", type=float, default=240, help="Observation budget, 15..600 seconds"
    )
    parser.add_argument(
        "--after-terminal", type=float, default=20, help="Subsequent window, 5..60 seconds"
    )
    parser.add_argument(
        "--pause-hold", type=float, help="Opt into one guarded pause/resume, 5..60 seconds"
    )
    parser.add_argument(
        "--code-revision", help="Operator-recorded tested Git revision, 7..40 hex characters"
    )
    args = parser.parse_args(argv)
    try:
        options = CheckpointOptions(args.seconds, args.after_terminal, args.pause_hold)
        if fullmatch(r"[A-Za-z0-9_-]{11}", args.content) is None:
            raise ValueError("Content must be an exact video ID.")
        if (
            args.code_revision is not None
            and fullmatch(r"[0-9a-fA-F]{7,40}", args.code_revision) is None
        ):
            raise ValueError("Code revision must be a Git commit identifier.")
        runtime = Path("runtime").resolve()
        if not args.device_config.resolve().is_relative_to(runtime):
            raise ValueError("Device configuration must be private under runtime/.")
        config = read(args.device_config)
        if config is None or not isinstance(config.get("device_id"), str):
            raise ValueError("Device configuration must supply device_id.")
        target = PlaybackTarget(str(UUID(config["device_id"])), "cast")
        request = PlaybackRequest(
            str(uuid4()), str(uuid4()), str(uuid4()), ContentRef("youtube", args.content), target
        )
        clock = SystemClock()
        from tellyq.cast_backend import open_backend

        with command_lock(runtime), CheckpointJournal(args.output, clock) as journal:
            journal.write(
                "environment",
                python=python_version(),
                gil_enabled=sys._is_gil_enabled(),
                pychromecast=version("PyChromecast"),
                casttube=version("casttube"),
                operator_code_revision=args.code_revision,
            )
            before = _hashes(runtime)
            journal.write("local_state_before", hashes=before)
            summary: CheckpointSummary | None = None
            try:
                with open_backend(target, clock, 6) as (backend, _device):
                    store = JsonSessionStore(args.output / "session-store", clock)
                    summary = run_checkpoint(
                        backend,
                        store,
                        clock,
                        journal,
                        request,
                        options,
                        drain_raw=backend.drain_raw_observations,
                        phase=_phase,
                    )
            except (Exception, KeyboardInterrupt) as exc:
                journal.write("connection_error", error_type=type(exc).__name__)
                if summary is None:
                    summary = CheckpointSummary()
                summary.stop_reason = (
                    "interrupted" if isinstance(exc, KeyboardInterrupt) else "connection_error"
                )
                summary.error_type = summary.error_type or type(exc).__name__
                journal.write("end", summary=summary)
            finally:
                after = _hashes(runtime)
                journal.write("local_state_after", hashes=after, unchanged=before == after)
            result = {**asdict(summary), "legacy_state_unchanged": before == after}
            save(args.output / "summary.json", result)
        print(json.dumps(result, allow_nan=False), flush=True)
        if summary.stop_reason == "interrupted":
            return 130
        return int(
            summary.stop_reason not in {"deadline", "post_terminal_window"}
            or summary.cleanup != "observed"
            or (
                args.pause_hold is not None
                and (summary.pause != "observed" or summary.resume != "observed")
            )
            or before != after
        )
    except KeyboardInterrupt:
        print(json.dumps({"error": "interrupted", "visual_confirmation": None}), flush=True)
        return 130
    except Exception:
        print(json.dumps({"error": "checkpoint_failed", "visual_confirmation": None}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
