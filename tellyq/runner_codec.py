"""Strict versioned request decoding and safe cached-state projection for local IPC."""

import json
from dataclasses import dataclass
from math import isfinite
from typing import Literal, cast

from .domain.values import CommandAction, ContentKind, ContentRef, PlaybackRequest, PlaybackTarget
from .handoff import HandoffDiagnostic, HandoffEvidence
from .models import (
    IPCAction,
    IPCCode,
    IPCCommand,
    IPCHandoffDiagnostic,
    IPCHandoffEvidence,
    IPCPlaybackRequest,
    IPCRequest,
    IPCResponse,
    IPCRunnerSnapshot,
    IPCTaskView,
    IPCTicket,
    IPCValue,
)
from .runner import RunnerCommand, RunnerSnapshot, TaskView, TicketSnapshot

MAX_FRAME_BYTES = 65_536
EXTERNAL_ACTIONS = frozenset({"start", "stop", "pause", "resume"})
_CODES = frozenset(
    {
        "status",
        "accepted",
        "ticket",
        "shutdown",
        "shutdown_timeout",
        "invalid_request",
        "command_conflict",
        "command_rejected",
        "runner_unavailable",
        "mailbox_full",
        "history_full",
        "ticket_missing",
        "server_busy",
        "response_too_large",
        "internal_error",
    }
)


class IPCProtocolError(ValueError):
    """Invalid local wire data; public errors deliberately omit supplied text."""


@dataclass(frozen=True, slots=True)
class DecodedRequest:
    operation: Literal["status", "submit", "ticket", "shutdown"]
    command: RunnerCommand | None = None
    command_id: str | None = None
    timeout: float = 0


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise IPCProtocolError("Duplicate object key.")
        result[key] = value
    return result


def _constant(_value: str) -> None:
    raise IPCProtocolError("Non-finite JSON value.")


def _json(value: bytes) -> object:
    if len(value) > MAX_FRAME_BYTES or not value.endswith(b"\n") or b"\n" in value[:-1]:
        raise IPCProtocolError("Expected one bounded JSON line.")
    try:
        return json.loads(value, object_pairs_hook=_pairs, parse_constant=_constant)
    except ValueError, UnicodeError, RecursionError:
        raise IPCProtocolError("Invalid JSON line.") from None


def _object(value: object, keys: set[str], optional: set[str] | None = None) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or set(value) - keys - (optional or set())
        or not keys <= set(value)
    ):
        raise IPCProtocolError("Object fields do not match the protocol.")
    return cast(dict[str, object], value)


def _text(value: object, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise IPCProtocolError("Invalid bounded string.")
    return value


def _id(value: object) -> str:
    result = _text(value)
    assert result is not None
    return result


def _playback(value: object) -> PlaybackRequest:
    fields = _object(value, {"request_id", "attempt_id", "queue_item_id", "content", "target"})
    content = _object(fields["content"], {"provider", "content_id", "kind", "title"})
    target = _object(fields["target"], {"device_id", "route", "name"})
    try:
        kind = ContentKind(_id(content["kind"]))
    except ValueError:
        raise IPCProtocolError("Unsupported content kind.") from None
    return PlaybackRequest(
        _id(fields["request_id"]),
        _id(fields["attempt_id"]),
        _id(fields["queue_item_id"]),
        ContentRef(
            _id(content["provider"]),
            _id(content["content_id"]),
            kind,
            _text(content["title"], nullable=True),
        ),
        PlaybackTarget(
            _id(target["device_id"]), _id(target["route"]), _text(target["name"], nullable=True)
        ),
    )


def decode_request(frame: bytes) -> DecodedRequest:
    fields = _object(
        _json(frame), {"schema_version", "operation"}, {"command", "command_id", "timeout"}
    )
    if type(fields["schema_version"]) is not int or fields["schema_version"] != 1:
        raise IPCProtocolError("Unsupported protocol version.")
    operation = fields["operation"]
    if operation == "status":
        _object(fields, {"schema_version", "operation"})
        return DecodedRequest("status")
    if operation == "ticket":
        _object(fields, {"schema_version", "operation", "command_id"})
        return DecodedRequest("ticket", command_id=_id(fields["command_id"]))
    if operation == "shutdown":
        _object(fields, {"schema_version", "operation", "timeout"})
        value = fields["timeout"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 5:
            raise IPCProtocolError("Shutdown wait must be between zero and five seconds.")
        return DecodedRequest("shutdown", timeout=float(value))
    if operation == "submit":
        _object(fields, {"schema_version", "operation", "command"})
        command = _object(fields["command"], {"command_id", "action", "request"})
        action = _id(command["action"])
        if action not in EXTERNAL_ACTIONS:
            raise IPCProtocolError("Unsupported external command.")
        request = _playback(command["request"]) if action == "start" else None
        if action != "start" and command["request"] is not None:
            raise IPCProtocolError("Only start accepts a playback request.")
        return DecodedRequest(
            "submit", RunnerCommand(_id(command["command_id"]), CommandAction(action), request)
        )
    raise IPCProtocolError("Unsupported operation.")


def wire_playback(request: PlaybackRequest) -> IPCPlaybackRequest:
    return {
        "request_id": request.request_id,
        "attempt_id": request.attempt_id,
        "queue_item_id": request.queue_item_id,
        "content": {
            "provider": request.content.provider,
            "content_id": request.content.content_id,
            "kind": request.content.kind.value,
            "title": request.content.title,
        },
        "target": {
            "device_id": request.target.device_id,
            "route": request.target.route,
            "name": request.target.name,
        },
    }


def _action(value: str) -> IPCAction:
    if value not in EXTERNAL_ACTIONS:
        raise IPCProtocolError("Unsupported external command.")
    return cast(IPCAction, value)


def wire_command(command: RunnerCommand) -> IPCCommand:
    if command.action.value not in EXTERNAL_ACTIONS:
        raise IPCProtocolError("Unsupported external command.")
    return {
        "command_id": command.command_id,
        "action": _action(command.action.value),
        "request": wire_playback(command.request) if command.request is not None else None,
    }


def encode_request(value: IPCRequest) -> bytes:
    encoded = _encode(value)
    decode_request(encoded)
    return encoded


def fingerprint(command: RunnerCommand) -> str:
    return json.dumps(wire_command(command), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _wire_handoff_evidence(evidence: HandoffEvidence) -> IPCHandoffEvidence:
    return {
        "sequence": evidence.sequence,
        "age_seconds": evidence.age_seconds,
        "since_terminal_seconds": evidence.since_terminal_seconds,
        "sequence_delta": evidence.sequence_delta,
        "observation_delta_seconds": evidence.observation_delta_seconds,
        "terminal_sequence": evidence.terminal_sequence,
        "terminal_position": evidence.terminal_position,
        "state": evidence.state.value,
        "idle_reason": evidence.idle_reason.value if evidence.idle_reason else None,
        "position": evidence.position,
        "ad_active": evidence.ad_active,
        "provider_phase": evidence.provider_phase.value if evidence.provider_phase else None,
        "identity_update": evidence.identity_update.value,
        "session_active": evidence.session_active,
        "connection_reset": evidence.connection_reset,
        "target_matches": evidence.target_matches,
        "connection_matches": evidence.connection_matches,
        "content_matches": evidence.content_matches,
        "session_matches": evidence.session_matches,
        "application_matches": evidence.application_matches,
        "media_matches": evidence.media_matches,
    }


def _wire_handoff(diagnostic: HandoffDiagnostic) -> IPCHandoffDiagnostic:
    return {
        "stage": diagnostic.stage.value,
        "disposition": diagnostic.disposition.value,
        "reason": diagnostic.reason.value,
        "queue_reason": diagnostic.queue_reason.value if diagnostic.queue_reason else None,
        "evidence": _wire_handoff_evidence(diagnostic.evidence) if diagnostic.evidence else None,
        "first_veto": {
            "reason": diagnostic.first_veto.reason.value,
            "evidence": _wire_handoff_evidence(diagnostic.first_veto.evidence),
        }
        if diagnostic.first_veto
        else None,
    }


def wire_view(view: TaskView) -> IPCTaskView:
    playback: dict[str, IPCValue] | None = None
    if (sample := view.playback) is not None:
        latest = sample.latest
        receipt = sample.receipt
        completion = sample.completion
        playback = {
            "attempt_id": sample.scope.request.attempt_id,
            "content_id": sample.scope.request.content.content_id,
            "state": sample.state.value,
            "revision": sample.revision,
            "ownership_lost": sample.ownership_lost,
            "stop_requested": sample.stop_requested,
            "release_requested": sample.release_boundary is not None,
            "release_confirmed": sample.release_confirmed,
            "observed_at": latest.observed_at.isoformat() if latest else None,
            "position": latest.position if latest else None,
            "duration": latest.duration if latest else None,
            "ad_active": latest.ad_active if latest else None,
            "evidence": {
                "identity_confirmed": sample.evidence.identity_confirmed,
                "receiver_playback_confirmed": sample.evidence.receiver_playback_confirmed,
                "natural_completion_confirmed": sample.evidence.natural_completion_confirmed,
                "reason": sample.evidence.reason.value,
            },
            "completion": {
                "historical": True,
                "observed_at": completion.observed_at.isoformat(),
                "sequence": completion.sequence,
                "identity_sequence": completion.identity_sequence,
                "source": completion.source,
                "attribution": completion.attribution.value,
            }
            if completion
            else None,
            "receipt": {
                "request_id": receipt.request_id,
                "action": receipt.action.value,
                "outcome": receipt.outcome.value,
                "recorded_at": receipt.recorded_at.isoformat(),
                "requested_at": receipt.requested_at.isoformat() if receipt.requested_at else None,
                "error_code": receipt.error.code.value if receipt.error else None,
                "uncertain": receipt.error.uncertain if receipt.error else False,
                "diagnostic": {
                    "stage": receipt.diagnostic.stage.value,
                    "reason": receipt.diagnostic.reason.value,
                }
                if receipt.diagnostic
                else None,
            }
            if receipt
            else None,
        }
    queue: dict[str, IPCValue] | None = None
    if (saved := view.queue) is not None:
        selected = list(saved.items[:128])
        if saved.current_item_id not in {item.item_id for item in selected}:
            current = next(
                (item for item in saved.items if item.item_id == saved.current_item_id), None
            )
            if current is not None:
                selected[-1] = current
        queue = {
            "queue_id": saved.queue_id,
            "target": {
                "device_id": saved.target.device_id,
                "route": saved.target.route,
                "name": saved.target.name,
            },
            "revision": saved.revision,
            "generation": saved.generation,
            "current_item_id": saved.current_item_id,
            "cancellation_requested": saved.cancellation_requested,
            "mode": saved.mode.value,
            "reconciliation_required": saved.reconciliation_required,
            "item_count": len(saved.items),
            "items_truncated": len(saved.items) > len(selected),
            "items": [
                {
                    "item_id": item.item_id,
                    "provider": item.content.provider,
                    "content_id": item.content.content_id,
                    "kind": item.content.kind.value,
                    "title": item.content.title,
                    "intent": item.intent.value,
                }
                for item in selected
            ],
            "native_reservations": [
                {
                    "reservation_id": reservation.reservation_id,
                    "predecessor_attempt_id": reservation.predecessor_attempt_id,
                    "successor_item_id": reservation.successor_item_id,
                    "successor_attempt_id": reservation.successor_attempt_id,
                    "state": reservation.state.value,
                }
                for reservation in saved.reservations[-32:]
            ],
            "native_operations": [
                {
                    "operation_id": operation.operation_id,
                    "reservation_id": operation.reservation_id,
                    "action": operation.action.value,
                    "state": operation.state.value,
                }
                for operation in saved.native_operations[-32:]
            ],
            "native_operations_truncated": len(saved.native_operations) > 32,
        }
    return {
        "playback": playback,
        "queue": queue,
        "handoff": _wire_handoff(view.handoff) if view.handoff else None,
    }


def wire_ticket(ticket: TicketSnapshot) -> IPCTicket:
    action = ticket.command.action.value
    if action not in EXTERNAL_ACTIONS:
        raise IPCProtocolError("Unsupported external command.")
    return {
        "command_id": ticket.command.command_id,
        "action": _action(action),
        "state": ticket.state.value,
        "failure": ticket.failure.value if ticket.failure else None,
        "view": wire_view(ticket.view) if ticket.view is not None else None,
    }


def wire_snapshot(snapshot: RunnerSnapshot) -> IPCRunnerSnapshot:
    return {
        "phase": snapshot.phase.value,
        "revision": snapshot.revision,
        "owns_device": snapshot.owns_device,
        "cancellation_requested": snapshot.cancellation_requested,
        "pending_commands": snapshot.pending_commands,
        "active_command_id": snapshot.active_command.command_id
        if snapshot.active_command
        else None,
        "last_command_id": snapshot.last_command.command.command_id
        if snapshot.last_command
        else None,
        "view": wire_view(snapshot.view),
        "failure": snapshot.failure.value if snapshot.failure else None,
    }


def error_response(code: IPCCode) -> IPCResponse:
    return {"schema_version": 1, "ok": False, "code": code}


def _encode(value: object) -> bytes:
    try:
        encoded = (json.dumps(value, allow_nan=False, separators=(",", ":")) + "\n").encode()
    except ValueError, TypeError, RecursionError:
        raise IPCProtocolError("Response cannot be represented as JSON.") from None
    if len(encoded) > MAX_FRAME_BYTES:
        raise IPCProtocolError("Response exceeds the frame limit.")
    return encoded


def encode_response(value: IPCResponse) -> bytes:
    return _encode(value)


def _json_tree(value: object, depth: int = 0) -> None:
    if depth > 16:
        raise IPCProtocolError("Response nesting exceeds the limit.")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float) and isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            _json_tree(item, depth + 1)
        return
    if isinstance(value, dict):
        for item in value.values():
            _json_tree(item, depth + 1)
        return
    raise IPCProtocolError("Invalid JSON response value.")


def _enum(value: object, allowed: set[str], *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or value not in allowed:
        raise IPCProtocolError("Unsupported response field value.")


def _failure(value: object) -> None:
    _enum(
        value,
        {
            "ownership_unavailable",
            "task_initialization_failed",
            "command_failed",
            "observation_step_failed",
            "task_close_failed",
        },
        nullable=True,
    )


def _view(value: object) -> None:
    fields = _object(value, {"playback", "queue"}, {"handoff"})
    if fields.get("handoff") is not None and not isinstance(fields["handoff"], dict):
        raise IPCProtocolError("Invalid handoff projection.")
    for name in ("playback", "queue"):
        if fields[name] is not None and not isinstance(fields[name], dict):
            raise IPCProtocolError("Invalid read-model projection.")


def _ticket(value: object) -> dict[str, object]:
    fields = _object(value, {"command_id", "action", "state", "failure", "view"})
    _id(fields["command_id"])
    _action(_id(fields["action"]))
    _enum(fields["state"], {"queued", "running", "handled", "canceled", "failed"})
    _failure(fields["failure"])
    if fields["view"] is not None:
        _view(fields["view"])
    return fields


def _snapshot(value: object) -> None:
    fields = _object(
        value,
        {
            "phase",
            "revision",
            "owns_device",
            "cancellation_requested",
            "pending_commands",
            "active_command_id",
            "last_command_id",
            "view",
            "failure",
        },
    )
    _enum(fields["phase"], {"new", "starting", "running", "stopping", "stopped", "failed"})
    for name in ("revision", "pending_commands"):
        if type(fields[name]) is not int or cast(int, fields[name]) < 0:
            raise IPCProtocolError("Invalid non-negative integer.")
    for name in ("owns_device", "cancellation_requested"):
        if type(fields[name]) is not bool:
            raise IPCProtocolError("Invalid boolean field.")
    for name in ("active_command_id", "last_command_id"):
        _text(fields[name], nullable=True)
    _failure(fields["failure"])
    _view(fields["view"])


def decode_response(frame: bytes) -> IPCResponse:
    raw = _object(
        _json(frame), {"schema_version", "ok", "code"}, {"snapshot", "ticket", "ticket_id"}
    )
    if (
        type(raw["schema_version"]) is not int
        or raw["schema_version"] != 1
        or type(raw["ok"]) is not bool
    ):
        raise IPCProtocolError("Invalid response envelope.")
    if not isinstance(raw["code"], str) or raw["code"] not in _CODES:
        raise IPCProtocolError("Unknown response code.")
    if raw["ok"] != (raw["code"] in {"accepted", "ticket", "status", "shutdown"}):
        raise IPCProtocolError("Inconsistent response outcome.")
    _json_tree(raw)
    code = raw["code"]
    if code in {"accepted", "ticket"}:
        _object(raw, {"schema_version", "ok", "code", "ticket", "ticket_id"})
        ticket = _ticket(raw["ticket"])
        if _id(raw["ticket_id"]) != ticket["command_id"]:
            raise IPCProtocolError("Inconsistent ticket identity.")
    elif code in {"status", "shutdown", "shutdown_timeout"}:
        _object(raw, {"schema_version", "ok", "code", "snapshot"})
        _snapshot(raw["snapshot"])
    else:
        _object(raw, {"schema_version", "ok", "code"})
    return cast(IPCResponse, raw)
