"""CLI composition and compatible JSON reporting around the injected application."""

import logging
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import fields
from itertools import pairwise
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import uuid4

from .application import PlaybackApplication, PlaybackResult
from .clock import SystemClock
from .domain.ports import Clock, PlaybackBackend, SessionStore
from .domain.values import (
    CommandOutcome,
    CommandReceipt,
    ContentRef,
    PlaybackRequest,
    PlaybackScope,
    PlaybackTarget,
    PlayerState,
    SessionSnapshot,
)
from .models import CommandReceipt as WireReceipt
from .models import Device, Observation, Report
from .programs import DEFAULT_CONTENT_ID, DEFAULT_ITEM_ID, DEFAULT_SERVICE, DEFAULT_TITLE
from .session_store import JsonSessionStore
from .state import command_lock, load_queue, make_queue, read, save

BackendFactory = Callable[
    [PlaybackTarget, Clock, float], AbstractContextManager[tuple[PlaybackBackend, Device]]
]


@runtime_checkable
class WireObservations(Protocol):
    @property
    def raw_observations(self) -> list[Observation]: ...


def _wire_receipt(receipt: CommandReceipt) -> WireReceipt:
    return {
        "action": "play_video" if receipt.action.value == "start" else "quit_app",
        "requested_at": (receipt.requested_at or receipt.recorded_at).isoformat(),
        "recorded_at": receipt.recorded_at.isoformat(),
        "returned": receipt.outcome == CommandOutcome.ACCEPTED,
        "outcome": receipt.outcome.value,
    }


def _observations(result: PlaybackResult) -> list[Observation]:
    return [
        {
            "kind": "receiver" if event.session_active is not None else "media",
            "observed_at": event.observed_at.isoformat(),
            "monotonic": event.monotonic,
            "content_id": event.content.content_id if event.content else None,
            "title": event.content.title if event.content else None,
            "player_state": event.state.value.upper(),
            "position": event.position,
            "duration": event.duration,
            "ad_break": event.ad_active,
            "idle_reason": event.idle_reason.value.upper() if event.idle_reason else None,
            "app_id": event.application_id,
            "app_session_id": event.session_id,
            "playback_id": event.playback_id,
            "sequence": event.sequence,
            "connection_generation": event.connection_generation,
            "source": event.source,
        }
        for event in result.observations
    ]


def _result_report(report: Report, result: PlaybackResult, clock: Clock) -> int:
    snapshot = result.snapshot
    report["observations"] = _observations(result)
    report["state"] = "unconfirmed"
    report["observed_state"] = snapshot.state.value if snapshot else "unknown"
    report["evidence"] = {
        "receiver_playback_confirmed": bool(
            snapshot and snapshot.evidence.receiver_playback_confirmed
        ),
        "identity_observed": bool(snapshot and snapshot.evidence.identity_confirmed),
        "playing_observed": bool(snapshot and snapshot.state == PlayerState.PLAYING),
        "advancing_position_observed": False,
        "natural_completion_confirmed": bool(
            snapshot and snapshot.evidence.natural_completion_confirmed
        ),
        "reason": result.reason.value,
        "visual_confirmation": None,
    }
    if snapshot is not None:
        report["attempt_id"] = snapshot.scope.request.attempt_id
        if snapshot.evidence.receiver_playback_confirmed and snapshot.state == PlayerState.PLAYING:
            report["state"] = "playing"
        elif snapshot.state == PlayerState.STOPPED:
            report["state"] = "stopped"
        elif snapshot.evidence.natural_completion_confirmed:
            report["state"] = "finished"
        # Position telemetry is distinct from ad-free playback verification.
        recent = [
            event
            for event in result.observations
            if event.target == snapshot.scope.request.target
            and event.connection_generation == snapshot.scope.connection_generation
            and event.session_id == snapshot.scope.session_id
            and event.content == snapshot.scope.request.content
            and event.state == PlayerState.PLAYING
            and event.position is not None
            and 0 <= clock.monotonic() - event.monotonic <= 5
        ]
        report["evidence"]["advancing_position_observed"] = not snapshot.ownership_lost and any(
            first.position is not None
            and second.position is not None
            and first.playback_id == second.playback_id
            and second.sequence > first.sequence
            and second.monotonic - first.monotonic >= 1
            and second.position > first.position
            for first, second in pairwise(recent)
        )
    receipt = result.receipt
    if receipt is not None:
        report["commands"] = [_wire_receipt(receipt)]
        if receipt.outcome != CommandOutcome.ACCEPTED:
            report["error"] = {
                "type": "CommandOutcomeUnknown"
                if receipt.outcome == CommandOutcome.UNKNOWN
                else "CommandRejected",
                "message": receipt.error.message
                if receipt.error
                else "The backend refused this command.",
            }
            return 1
    if result.failure is not None:
        report["error"] = {
            "type": "ObservationOrPersistenceError",
            "message": result.failure.message,
        }
        report["state"] = "unconfirmed"
        return 1
    return 0


def _legacy_snapshot(
    runtime: Path, request: PlaybackRequest, clock: Clock
) -> SessionSnapshot | None:
    legacy = read(runtime / "session.json")
    if legacy is None:
        return None
    target = PlaybackTarget(legacy["device_id"], "cast")
    owned_request = PlaybackRequest(
        request.request_id,
        request.attempt_id,
        request.queue_item_id,
        ContentRef(DEFAULT_SERVICE, legacy["content_id"]),
        target,
    )
    return SessionSnapshot(
        PlaybackScope(
            owned_request,
            legacy["app_session_id"],
            str(uuid4()),
            clock.monotonic(),
            legacy["app_id"],
        )
    )


def execute(
    command: str,
    runtime: Path,
    device_id: str | None = None,
    seconds: float = 30,
    *,
    backend_factory: BackendFactory | None = None,
    clock: Clock | None = None,
    store: SessionStore | None = None,
    ownership: Callable[[], SessionSnapshot | None] | None = None,
    discover: Callable[[], list[Device]] | None = None,
) -> tuple[Report, int]:
    clock = clock or SystemClock()
    durable = JsonSessionStore(runtime, clock)
    store = store or durable
    report: Report = {
        "schema_version": 1,
        "command": command,
        "started_at": clock.utcnow().isoformat(),
        "python": sys.version,
        "gil_enabled": sys._is_gil_enabled(),
        "commands": [],
        "observations": [],
    }
    path = runtime / "runs" / f"{uuid4()}.json"
    report["report_path"] = str(path)
    device_io = False
    code = 0
    queue = None
    update_queue = False
    with command_lock(runtime):
        try:
            if command not in {"discover", "queue", "probe", "start", "status", "stop"}:
                raise ValueError("Unknown command.")
            if not 5 <= seconds <= 120:
                raise ValueError("Observation window must be between 5 and 120 seconds.")
            if command == "discover":
                if discover is None:
                    from .cast_backend import discover_devices

                    discover = discover_devices
                device_io = True
                report["devices"] = discover()
                save(runtime / "discovery.json", report)
            elif command == "queue":
                if device_id is None:
                    raise ValueError("Select an explicit device UUID from discover.")
                queue = make_queue(device_id)
                queue["updated_at"] = clock.utcnow().isoformat()
                save(runtime / "queue.json", queue)
                report["queue"] = queue
                report["state"] = "queued"
            else:
                # Validate both legacy files and the new snapshot before any device I/O.
                queue = load_queue(runtime) if (runtime / "queue.json").exists() else None
                legacy = read(runtime / "session.json")
                owned = ownership() if ownership is not None else durable.current()
                if command == "start":
                    queue = load_queue(runtime)
                    device_id = queue["device_id"]
                    if queue["items"][0]["state"] not in {
                        "queued",
                        "stopped",
                        "finished",
                        "failed",
                    }:
                        raise ValueError(
                            "This queue was already started; use status or stop before requeueing."
                        )
                device_id = (
                    device_id
                    or (owned.scope.request.target.device_id if owned else None)
                    or (legacy or {}).get("device_id")
                    or (queue or {}).get("device_id")
                )
                if not device_id:
                    raise ValueError("Select an explicit device UUID from discover.")
                target = PlaybackTarget(device_id, "cast")
                item = queue["items"][0] if queue is not None else None
                content = ContentRef(
                    item["service"] if item else DEFAULT_SERVICE,
                    item["content_id"] if item else DEFAULT_CONTENT_ID,
                    title=item["title"] if item else DEFAULT_TITLE,
                )
                request = PlaybackRequest(
                    str(uuid4()),
                    str(uuid4()),
                    item["id"] if item else DEFAULT_ITEM_ID,
                    content,
                    target,
                )
                owned = owned or _legacy_snapshot(runtime, request, clock)
                if (
                    command in {"status", "stop"}
                    and owned is not None
                    and owned.scope.request.target == target
                ):
                    request = owned.scope.request
                if backend_factory is None:
                    from .cast_backend import open_backend

                    backend_factory = open_backend
                device_io = True
                with backend_factory(target, clock, seconds) as (backend, device):
                    report["device"] = device
                    report["requested_content_id"] = content.content_id
                    capabilities = backend.capabilities(target)
                    report["capabilities"] = {
                        field.name: getattr(capabilities, field.name).support.value
                        for field in fields(capabilities)
                    }

                    def record_receipt(receipt: CommandReceipt) -> None:
                        report["commands"] = [_wire_receipt(receipt)]
                        save(path, report)

                    application = PlaybackApplication(backend, store, clock, record_receipt)
                    if command in {"start", "probe"}:
                        if command == "start" and queue is not None:
                            queue["items"][0]["state"] = "starting"
                            save(runtime / "queue.json", queue)
                            update_queue = True
                        result = application.start(request)
                    elif command == "status":
                        result = application.status(request, owned)
                        update_queue = bool(
                            queue
                            and queue["device_id"] == device_id
                            and queue["items"][0]["state"] in {"starting", "playing", "unconfirmed"}
                        )
                    else:
                        # Persist uncertainty before the remote effect, including interrupted calls.
                        # Refused ownership reconciliation has no remote effect, but stale playing is unsafe.
                        if queue is not None and queue["device_id"] == device_id:
                            queue["items"][0]["state"] = "unconfirmed"
                            save(runtime / "queue.json", queue)
                            update_queue = True
                        result = application.stop(request, owned)
                    code = _result_report(report, result, clock)
                    if isinstance(backend, WireObservations):
                        report["observations"] = [
                            event.copy() for event in backend.raw_observations
                        ]
                    if command == "stop":
                        if report["state"] != "stopped":
                            report["state"] = "unconfirmed"
                        report["stop_confirmed"] = bool(
                            result.snapshot and result.snapshot.state == PlayerState.STOPPED
                        )
                        report["stop_verification_source"] = (
                            "receiver_app_exit" if report["stop_confirmed"] else None
                        )
        except (Exception, KeyboardInterrupt) as exc:
            logging.getLogger(__name__).exception("Playback operation failed")
            report["error"] = {
                "type": type(exc).__name__,
                "message": "Playback operation failed; inspect local diagnostics."
                if device_io
                else str(exc),
            }
            report.setdefault(
                "state", "unconfirmed" if update_queue or report["commands"] else "failed"
            )
            code = 1
        report["ended_at"] = clock.utcnow().isoformat()
        try:
            save(path, report)
        except OSError:
            report["error"] = {
                "type": "PersistenceError",
                "message": "Could not save the local run report.",
            }
            code = 1
        if queue is not None and update_queue:
            queue["items"][0]["state"] = report["state"]
            queue["updated_at"] = clock.utcnow().isoformat()
            queue["last_report"] = str(path)
            try:
                save(runtime / "queue.json", queue)
            except OSError:
                report["error"] = {
                    "type": "PersistenceError",
                    "message": "Could not save the local queue state.",
                }
                code = 1
        return report, code
