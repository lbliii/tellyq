"""Bounded, read-only current-message customData inspection; never controls playback."""

import argparse
import json
import sys
from contextlib import ExitStack
from math import isfinite
from pathlib import Path
from typing import Literal
from uuid import UUID

from tellyq.cast_backend import CastBackend
from tellyq.clock import SystemClock
from tellyq.domain.ports import Clock
from tellyq.domain.values import PlaybackTarget
from tellyq.models import YouTubeMetadataRecord
from tellyq.youtube_metadata import MetadataJournal, YouTubeMetadataProbe


def capture(
    backend: CastBackend,
    *,
    target: PlaybackTarget,
    probe: YouTubeMetadataProbe,
    clock: Clock,
    journal: MetadataJournal,
    private_journal: MetadataJournal | None,
    seconds: float,
) -> YouTubeMetadataRecord:
    if not backend.read_only:
        raise ValueError("Metadata capture requires an observation-only backend.")
    if not isfinite(seconds) or not 0 < seconds <= 14_400:
        raise ValueError("Capture duration must be positive and at most 14400 seconds.")
    if probe.private_schema != (private_journal is not None):
        raise ValueError("Private schema capture requires a separate private journal.")
    start = clock.monotonic()
    begin: YouTubeMetadataRecord = {
        "schema_version": 1,
        "kind": "begin",
        "observed_at": clock.utcnow().isoformat(),
        "read_only": True,
        "completion_authorized": False,
        "seconds": seconds,
    }
    journal.write(begin)
    if private_journal is not None:
        private_journal.write(begin)
    print(json.dumps({"capturing": True, "read_only": True}), file=sys.stderr, flush=True)
    samples = 0
    dropped = 0

    def flush() -> None:
        nonlocal samples, dropped
        pending, dropped = probe.drain()
        for sample, schema in pending:
            record: YouTubeMetadataRecord = {
                "schema_version": 1,
                "kind": "sample",
                "observed_at": sample["observed_at"],
                "read_only": True,
                "completion_authorized": False,
                "sample": sample,
            }
            journal.write(record)
            if schema is not None and private_journal is not None:
                del record["sample"]
                record["private_schema"] = schema
                private_journal.write(record)
            samples += 1

    reason: Literal["deadline", "interrupted", "backend_error"] = "deadline"
    try:
        while (remaining := start + seconds - clock.monotonic()) > 0:
            backend.observe_window(target, min(2, remaining))
            backend.drain_raw_observations()
            flush()
    except KeyboardInterrupt:
        reason = "interrupted"
    except Exception:
        reason = "backend_error"
    # Queued projections remain diagnostic data even after interrupted transport.
    flush()
    end: YouTubeMetadataRecord = {
        "schema_version": 1,
        "kind": "end",
        "observed_at": clock.utcnow().isoformat(),
        "read_only": True,
        "completion_authorized": False,
        "stop_reason": reason,
        "samples": samples,
        "dropped_samples": dropped,
    }
    journal.write(end)
    if private_journal is not None:
        private_journal.write(end)
    return end


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True, type=UUID)
    parser.add_argument("--content", required=True, help="Exact YouTube video ID")
    parser.add_argument("--seconds", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New JSONL path under runtime/")
    parser.add_argument(
        "--private-schema-output",
        type=Path,
        help="Optional private, untrusted field-name inventory",
    )
    args = parser.parse_args(argv)
    try:
        if not isfinite(args.seconds) or not 0 < args.seconds <= 14_400:
            raise ValueError("Invalid capture duration.")
        probe = YouTubeMetadataProbe(
            args.content, private_schema=args.private_schema_output is not None
        )
        from tellyq.cast_backend import open_backend

        clock = SystemClock()
        target = PlaybackTarget(str(args.device), "cast")
        with ExitStack() as stack:
            journal = stack.enter_context(MetadataJournal(args.output))
            private_journal = (
                stack.enter_context(MetadataJournal(args.private_schema_output))
                if args.private_schema_output is not None
                else None
            )
            backend, _device = stack.enter_context(
                open_backend(target, clock, 2, read_only=True, metadata_probe=probe)
            )
            end = capture(
                backend,
                target=target,
                probe=probe,
                clock=clock,
                journal=journal,
                private_journal=private_journal,
                seconds=args.seconds,
            )
        print(json.dumps(end, allow_nan=False))
        return (
            130
            if end["stop_reason"] == "interrupted"
            else int(end["stop_reason"] == "backend_error")
        )
    except KeyboardInterrupt:
        print(json.dumps({"error": "Metadata capture interrupted; no playback command was sent."}))
        return 130
    except Exception:
        print(
            json.dumps({"error": "Metadata capture failed; check paths, arguments and receiver."})
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
