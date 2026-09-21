"""Pure normalization of untrusted Cast JSON into owned scalar observations.

No fields are carried forward from previous messages. The transport supplies
timestamps after parsing; correlation and playback decisions belong to policy.
"""

from math import isfinite
from re import fullmatch
from typing import TypeGuard

from .models import Observation

_PLAYER_STATES = frozenset({"IDLE", "PLAYING", "PAUSED", "BUFFERING", "LOADING"})
_IDLE_REASONS = frozenset({"CANCELLED", "INTERRUPTED", "FINISHED", "ERROR"})


def _is_object(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _field(value: object, name: str) -> object:
    return value.get(name) if _is_object(value) else None


def _first(value: object) -> object:
    return value[0] if isinstance(value, list) and value else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _boolean(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _integer(value: object) -> int | None:
    # bool is an int subclass but is never a Cast session ID or error code.
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _seconds(value: object) -> float | None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except OverflowError:
        return None
    return seconds if isfinite(seconds) and seconds >= 0 else None


def _choice(value: object, choices: frozenset[str]) -> str | None:
    return value if isinstance(value, str) and value in choices else None


def _ad_break(status: object) -> bool | None:
    """Recognize reported break activity; absence does not establish no ads."""
    current = _field(status, "breakStatus")
    if any(_text(_field(current, name)) is not None for name in ("breakId", "breakClipId")):
        return True
    if any(
        _seconds(_field(current, name)) is not None
        for name in ("currentBreakTime", "currentBreakClipTime")
    ):
        return True
    return None


def _pause_supported(status: object) -> bool | None:
    commands = _integer(_field(status, "supportedMediaCommands"))
    return bool(commands & 1) if commands is not None and commands >= 0 else None


def media_observation(data: object) -> Observation:
    """Extract the first media status, preserving missing or invalid fields."""
    statuses = _field(data, "status")
    if isinstance(statuses, list) and not statuses:
        # Preserve the existing public shape for explicit empty media status.
        return {"kind": "media", "empty_status": True}
    status = _first(statuses)
    media = _field(status, "media")
    if media is None:
        media = _field(_field(status, "extendedStatus"), "media")
    session_id = _integer(_field(status, "mediaSessionId"))
    return {
        "kind": "media",
        "empty_status": False if isinstance(status, dict) else None,
        "media_session_id": session_id if session_id is not None and session_id >= 0 else None,
        "content_id": _text(_field(media, "contentId")),
        "title": _text(_field(_field(media, "metadata"), "title")),
        "player_state": _choice(_field(status, "playerState"), _PLAYER_STATES),
        "position": _seconds(_field(status, "currentTime")),
        "duration": _seconds(_field(media, "duration")),
        "idle_reason": _choice(_field(status, "idleReason"), _IDLE_REASONS),
        "ad_break": _ad_break(status),
        "pause_supported": _pause_supported(status),
    }


def _solicited_idle_status(data: object, request_id: int | None) -> bool:
    """Accept the no-app GET_STATUS shape only with transport-owned correlation.

    Open Screen omits applications when idle, but still supplies userEq and
    volume. Missing metadata in an unsolicited/partial update is not app exit.
    """
    if (
        _integer(request_id) is None
        or request_id is None
        or request_id <= 0
        or _integer(_field(data, "requestId")) != request_id
    ):
        return False
    status = _field(data, "status")
    if not _is_object(status) or "applications" in status:
        return False
    volume = _field(status, "volume")
    level = _seconds(_field(volume, "level"))
    return (
        _is_object(_field(status, "userEq"))
        and level is not None
        and level <= 1
        and _boolean(_field(volume, "muted")) is not None
        and all(
            field not in status or _boolean(status[field]) is not None
            for field in ("isActiveInput", "isStandBy")
        )
    )


def _receiver_observation(data: object, request_id: int | None) -> Observation:
    status = _field(data, "status")
    apps = _field(status, "applications")
    app = _first(apps)
    app_id = _text(_field(app, "appId"))
    session_id = _text(_field(app, "sessionId"))
    if not isinstance(status, dict):
        reason = "INVALID_STATUS"
    elif not isinstance(apps, list) and not _solicited_idle_status(data, request_id):
        reason = "INVALID_APPLICATIONS"
    elif apps and (app_id is None or session_id is None):
        reason = "INVALID_APPLICATION_IDENTITY"
    else:
        reason = None
    if reason is not None:
        # The legacy stop policy interprets receiver app_id=None as app exit.
        # Require an explicit empty list or a correlated, validated idle reply.
        return {"kind": "error", "type": "INVALID_RECEIVER_STATUS", "reason": reason, "code": None}
    return {
        "kind": "receiver",
        "app_id": app_id,
        "app_name": _text(_field(app, "displayName")),
        "app_session_id": session_id,
        "active_input": _boolean(_field(status, "isActiveInput")),
        "standby": _boolean(_field(status, "isStandBy")),
    }


def normalize_message(
    data: object, *, receiver_status_request_id: int | None = None
) -> Observation | None:
    """Return a fresh scalar-only record, or None for an unrecognized message.

    Only allowlisted fields cross the callback boundary. Error reasons retain
    symbolic protocol codes, never free-form diagnostics or credential URLs.
    The optional request ID must come from a current GET_STATUS poll owned by
    the transport, never from an untrusted incoming message alone.
    """
    message_type = _text(_field(data, "type"))
    if message_type == "MEDIA_STATUS":
        return media_observation(data)
    if message_type == "RECEIVER_STATUS":
        return _receiver_observation(data, receiver_status_request_id)
    if message_type in {"LOAD_FAILED", "LAUNCH_ERROR"}:
        reason = _text(_field(data, "reason"))
        return {
            "kind": "error",
            "type": message_type,
            "reason": reason if reason and fullmatch(r"[A-Z][A-Z0-9_]{0,63}", reason) else None,
            "code": _integer(_field(data, "detailedErrorCode")),
        }
    return None
