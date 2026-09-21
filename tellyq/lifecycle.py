"""Passive lifecycle diagnostics with bounded memory and incremental private journals.

A capture preserves normalized adapter messages, not original Cast protocol frames.
It never starts, stops or seeks media, and cannot establish that no other controller
sought or stopped playback. Natural-run acceptance requires separate operator evidence.
"""

import json
import os
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from hashlib import sha256
from hmac import new as hmac_new
from math import isfinite
from pathlib import Path
from secrets import token_bytes
from threading import Event
from types import TracebackType
from typing import Literal, Protocol
from uuid import uuid4

from .cast_backend import CastBackend, video_id
from .completion_report import completion_report
from .domain import policy
from .domain.ports import Clock
from .domain.values import (
    ContentRef,
    PlaybackObservation,
    PlaybackRequest,
    PlaybackScope,
    PlaybackTarget,
    SessionSnapshot,
    require_instant,
)
from .models import (
    Evidence,
    LifecycleInspection,
    LifecycleRecord,
    Observation,
    WireFieldShape,
)
from .youtube_state import YOUTUBE_APPLICATION_ID

_WIRE_FIELDS = (
    "wire_media",
    "wire_media_content_id",
    "wire_extended_status",
    "wire_extended_media",
    "wire_extended_content_id",
    "wire_break_status",
    "wire_break_id",
    "wire_break_clip_id",
    "wire_break_time",
    "wire_break_clip_time",
    "wire_status_custom_data",
    "wire_media_custom_data",
    "wire_extended_media_custom_data",
    "wire_media_breaks",
    "wire_media_break_clips",
    "wire_current_item_id",
    "wire_loading_item_id",
    "wire_preloaded_item_id",
)
_WIRE_SHAPES: dict[str, WireFieldShape] = {
    value: value for value in ("absent", "null", "valid", "invalid", "unavailable")
}


class LifecycleBackend(Protocol):
    """Observation-only adapter; each call must honor its bounded window."""

    def observe_window(
        self, target: PlaybackTarget, seconds: float
    ) -> tuple[PlaybackObservation, ...]: ...

    def drain_raw_observations(self) -> list[Observation]: ...


class LifecycleSink(Protocol):
    def write(self, record: LifecycleRecord) -> None: ...


class JsonlJournal:
    """Create a private, exclusive journal and fsync every complete line.

    An interrupted final write can leave one incomplete line. Earlier lines remain
    independently readable. Opening a new capture never truncates an old capture.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._file = os.fdopen(fd, "w", encoding="utf-8")

    def write(self, record: LifecycleRecord) -> None:
        self._file.write(json.dumps(record, allow_nan=False, separators=(",", ":")) + "\n")
        self._file.flush()
        os.fsync(self._file.fileno())

    def __enter__(self) -> JsonlJournal:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._file.close()


class LifecycleTracker:
    """One passive scope across observation windows, also usable for synthetic replay.

    A fresh exact-content sample selects the scope once. A later takeover or
    disconnect stays lost; delayed packets cannot attach the capture again.
    """

    def __init__(self, request: PlaybackRequest, started: float) -> None:
        require_instant(started, "capture start")
        self.request = request
        self.started = started
        self.snapshot: SessionSnapshot | None = None

    def observe(
        self, events: Iterable[PlaybackObservation], *, now: float
    ) -> tuple[PlaybackObservation, ...]:
        """Reduce events and return only fresh, policy-accepted matching media samples."""
        require_instant(now, "current time")
        accepted: list[PlaybackObservation] = []
        for event in events:
            if self.snapshot is None:
                if (
                    event.target != self.request.target
                    or event.content != self.request.content
                    or event.session_id is None
                    or event.application_id is None
                    or event.connection_reset
                    or event.monotonic <= self.started
                    or not 0 <= now - event.monotonic <= 5
                ):
                    continue
                self.snapshot = SessionSnapshot(
                    PlaybackScope(
                        self.request,
                        event.session_id,
                        event.connection_generation,
                        self.started,
                        event.application_id,
                    )
                )
            previous = self.snapshot
            self.snapshot = policy.observe(previous, event, now=now)
            if (
                self.snapshot.latest is event
                and previous.latest is not event
                and not self.snapshot.ownership_lost
                and event.content == self.request.content
                and event.session_id == self.snapshot.scope.session_id
                and event.session_active is not True
            ):
                accepted.append(event)
        if self.snapshot is not None:
            self.snapshot = policy.refresh(self.snapshot, now=now)
        return tuple(accepted)

    def evidence(self) -> Evidence:
        value = self.snapshot.evidence if self.snapshot is not None else None
        return {
            "receiver_playback_confirmed": value.receiver_playback_confirmed if value else False,
            "identity_observed": value.identity_confirmed if value else False,
            "natural_completion_confirmed": value.natural_completion_confirmed if value else False,
            **completion_report(self.snapshot),
            "visual_confirmation": None,
            "reason": value.reason.value if value else "awaiting_observation",
        }


def capture_lifecycle(
    backend: LifecycleBackend,
    *,
    target: PlaybackTarget,
    content: ContentRef,
    clock: Clock,
    sink: LifecycleSink,
    seconds: float,
    provenance: Literal["live", "synthetic"] = "live",
    cancelled: Callable[[], bool] = lambda: False,
    wait: Callable[[float], object] | None = None,
) -> LifecycleRecord:
    """Journal a bounded passive capture; timeout and interruption never mean ended.

    Backend exceptions are recorded without their potentially private text. Sink
    failures propagate: a failed disk write must not look like a completed capture.
    """
    require_instant(seconds, "capture duration")
    if isinstance(seconds, bool) or not 0 < seconds <= 14_400:
        raise ValueError("Capture duration must be positive and at most four hours.")
    if provenance not in {"live", "synthetic"}:
        raise ValueError("Capture provenance must be live or synthetic.")
    wait = wait or Event().wait
    capture_id = str(uuid4())
    started = clock.monotonic()
    deadline = started + seconds
    tracker = LifecycleTracker(
        PlaybackRequest(capture_id, capture_id, capture_id, content, target), started
    )
    sequence = 0

    def record(kind: Literal["begin", "window", "gap", "end"]) -> LifecycleRecord:
        nonlocal sequence
        value: LifecycleRecord = {
            "schema_version": 1,
            "kind": kind,
            "capture_id": capture_id,
            "provenance": provenance,
            "sequence": sequence,
            "recorded_at": clock.utcnow().isoformat(),
            "monotonic": clock.monotonic(),
            "mode": "passive",
            "entire_run_observed": False,
            "external_control_unobserved": True,
            "attached": tracker.snapshot is not None,
            "state": tracker.snapshot.state.value if tracker.snapshot else "unknown",
            "evidence": tracker.evidence(),
        }
        sequence += 1
        return value

    begin = record("begin")
    begin["device"] = {"uuid": target.device_id, "name": target.name}
    begin["requested_content_id"] = content.content_id
    sink.write(begin)
    last_samples = {"transport": started, "media": started}
    gap_reported = {"transport": False, "media": False}
    reason: Literal["deadline", "interrupted", "backend_error", "cancelled"] = "deadline"
    while clock.monotonic() < deadline:
        if cancelled():
            reason = "cancelled"
            break
        before = clock.monotonic()
        window = min(2.0, deadline - before)
        try:
            events = backend.observe_window(target, window)
        except KeyboardInterrupt:
            reason = "interrupted"
            break
        except Exception:
            reason = "backend_error"
            break
        now = clock.monotonic()
        accepted_media = tracker.observe(events, now=now)
        for gap_kind in ("transport", "media"):
            last_sample = last_samples[gap_kind]
            samples = events if gap_kind == "transport" else accepted_media
            sample_times = sorted(
                {
                    event.monotonic
                    for event in samples
                    if event.target == target and last_sample < event.monotonic <= now
                }
            )
            for instant in (*sample_times, now):
                if instant - last_sample > 5 and not gap_reported[gap_kind]:
                    gap = record("gap")
                    gap["gap_seconds"] = instant - last_sample
                    gap["gap_kind"] = gap_kind
                    sink.write(gap)
                    gap_reported[gap_kind] = True
                if instant in sample_times:
                    last_sample = instant
                    gap_reported[gap_kind] = False
            last_samples[gap_kind] = last_sample
        value = record("window")
        value["observations"] = backend.drain_raw_observations()
        sink.write(value)
        remaining = min(window - (now - before), deadline - now)
        if remaining > 0:
            try:
                wait(remaining)
            except KeyboardInterrupt:
                reason = "interrupted"
                break
    # Preserve any normalized messages left by an interrupted/failed adapter call.
    tail = backend.drain_raw_observations()
    if tail:
        value = record("window")
        value["partial"] = True
        value["observations"] = tail
        sink.write(value)
    tracker.observe((), now=clock.monotonic())
    end = record("end")
    end["stop_reason"] = reason
    sink.write(end)
    return end


class CaptureSanitizer:
    """Allowlist scalar diagnostics; pseudonyms are stable only within one export.

    No mapping grows with the number of sessions. HMAC prevents public exports
    from exposing low-entropy identifiers through an unsalted hash dictionary.
    """

    def __init__(self) -> None:
        self._key = token_bytes(32)
        self._monotonic: float | None = None
        self._utc: datetime | None = None
        self._provenance: str | None = None
        self._capture_id: str | None = None
        self._sequence = -1
        self._last_monotonic: float | None = None
        self._ended = False

    def _identifier(self, category: str, value: object) -> str | None:
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            return None
        digest = hmac_new(self._key, f"{category}:{value}".encode(), sha256).hexdigest()[:24]
        return f"{category}-{digest}"

    def _instant(self, value: object) -> float | None:
        number = _number(value)
        if number is None or self._monotonic is None:
            return None
        return number - self._monotonic

    def _timestamp(self, value: object) -> str | None:
        if not isinstance(value, str) or self._utc is None:
            return None
        try:
            stamp = datetime.fromisoformat(value)
            if stamp.tzinfo is None:
                return None
            return (datetime(2000, 1, 1, tzinfo=UTC) + (stamp - self._utc)).isoformat()
        except ValueError, OverflowError:
            return None

    def observation(self, raw: Mapping[str, object]) -> Observation:
        kind = raw.get("kind")
        result: Observation = {
            "kind": kind
            if isinstance(kind, str) and kind in {"receiver", "media", "error"}
            else "unknown"
        }
        for key in _WIRE_FIELDS:
            shape = raw.get(key)
            if isinstance(shape, str) and shape in _WIRE_SHAPES:
                result[key] = _WIRE_SHAPES[shape]
        count = raw.get("wire_status_count")
        if type(count) is int and 0 <= count <= 65_536:
            result["wire_status_count"] = count
        elif "wire_status_count" in raw:
            result["wire_status_count"] = None
        shape = raw.get("custom_player_state_shape")
        if isinstance(shape, str) and shape in _WIRE_SHAPES:
            result["custom_player_state_shape"] = _WIRE_SHAPES[shape]
        if "custom_player_state" in raw:
            code = raw["custom_player_state"]
            result["custom_player_state"] = (
                code if type(code) is int and -(2**31) <= code < 2**31 else None
            )
        for key in ("position", "duration"):
            if key in raw:
                result[key] = _number(raw[key])
        for key in ("empty_status", "ad_break", "active_input", "standby", "pause_supported"):
            if key in raw:
                value = raw[key]
                result[key] = value if type(value) is bool else None
        for key, category in (
            ("app_session_id", "session"),
            ("app_id", "application"),
        ):
            if key in raw:
                result[key] = (
                    YOUTUBE_APPLICATION_ID
                    if key == "app_id" and raw[key] == YOUTUBE_APPLICATION_ID
                    else self._identifier(category, raw[key])
                )
        if "playback_id" in raw:
            playback = self._identifier("playback", raw["playback_id"])
            result["playback_id"] = str(int(playback[9:21], 16)) if playback else None
        if "content_id" in raw:
            content = raw["content_id"]
            result["content_id"] = self._identifier(
                "content", video_id(content) if isinstance(content, str) else None
            )
        generation = self._identifier("connection", raw.get("connection_generation"))
        if generation is not None:
            result["connection_generation"] = generation
        # Keep media IDs numeric because Cast normalization expects that wire type.
        media_id = raw.get("media_session_id")
        if "media_session_id" in raw:
            pseudonym = self._identifier("playback", media_id)
            result["media_session_id"] = int(pseudonym[9:21], 16) if pseudonym else None
        if "app_name" in raw:
            result["app_name"] = "YouTube" if raw["app_name"] == "YouTube" else None
        for key, values in (
            ("player_state", {"PLAYING", "PAUSED", "BUFFERING", "LOADING", "IDLE", "UNKNOWN"}),
            ("idle_reason", {"FINISHED", "CANCELED", "CANCELLED", "INTERRUPTED", "ERROR"}),
            ("source", {"receiver", "receiver_status", "cast_media"}),
            (
                "type",
                {
                    "CONNECTION_RESET",
                    "INVALID_RECEIVER_STATUS",
                    "LOAD_FAILED",
                    "LAUNCH_ERROR",
                    "LOAD_CANCELLED",
                    "INVALID_REQUEST",
                },
            ),
            (
                "reason",
                {
                    "LOST",
                    "DISCONNECTED",
                    "CONNECTING",
                    "UNCORRELATED_APP_ABSENCE",
                    "APP_NOT_FOUND",
                    "INVALID_STATUS",
                    "INVALID_APPLICATIONS",
                    "INVALID_APPLICATION_IDENTITY",
                },
            ),
        ):
            value = raw.get(key)
            if isinstance(value, str) and value in values:
                result[key] = value
        for key in ("sequence", "code"):
            value = raw.get(key)
            if type(value) is int and 0 <= value <= 2**53:
                result[key] = value
        instant = self._instant(raw.get("monotonic"))
        if instant is not None:
            result["monotonic"] = instant
        stamp = self._timestamp(raw.get("observed_at"))
        if stamp is not None:
            result["observed_at"] = stamp
        return result

    def record(self, raw: Mapping[str, object]) -> LifecycleRecord:
        kind_value = raw.get("kind")
        kinds: dict[str, Literal["begin", "window", "gap", "end"]] = {
            value: value for value in ("begin", "window", "gap", "end")
        }
        kind = kinds.get(kind_value) if isinstance(kind_value, str) else None
        provenance = raw.get("provenance")
        stamp = raw.get("recorded_at")
        instant = _number(raw.get("monotonic"))
        sequence = raw.get("sequence")
        capture_id = raw.get("capture_id")
        if (
            type(raw.get("schema_version")) is not int
            or raw.get("schema_version") != 1
            or kind is None
            or not isinstance(provenance, str)
            or provenance not in {"live", "synthetic", "sanitized-live"}
            or not isinstance(stamp, str)
            or instant is None
            or type(sequence) is not int
            or sequence < 0
            or not isinstance(capture_id, str)
            or not capture_id
        ):
            raise ValueError("Invalid lifecycle journal record.")
        if self._monotonic is None:
            if kind != "begin":
                raise ValueError("Lifecycle export requires its begin record.")
            self._monotonic = instant
            try:
                self._utc = datetime.fromisoformat(stamp)
            except ValueError:
                raise ValueError("Invalid lifecycle timestamp.") from None
            if self._utc.tzinfo is None:
                raise ValueError("Lifecycle timestamps need a timezone.")
            self._provenance = provenance
            self._capture_id = capture_id
        elif kind == "begin":
            raise ValueError("A journal cannot contain a second begin record.")
        if self._provenance != provenance:
            raise ValueError("A journal cannot mix live and synthetic evidence.")
        if (
            self._capture_id != capture_id
            or sequence <= self._sequence
            or self._ended
            or (self._last_monotonic is not None and instant < self._last_monotonic)
        ):
            raise ValueError("Lifecycle journal identity or record ordering changed.")
        shifted_stamp = self._timestamp(stamp)
        if shifted_stamp is None:
            raise ValueError("Invalid lifecycle timestamp.")
        self._sequence = sequence
        self._last_monotonic = instant
        self._ended = kind == "end"
        record: LifecycleRecord = {
            "schema_version": 1,
            "kind": kind,
            "capture_id": self._identifier("capture", raw.get("capture_id")) or "capture-unknown",
            "provenance": "synthetic" if provenance == "synthetic" else "sanitized-live",
            "sequence": sequence,
            "recorded_at": shifted_stamp,
            "monotonic": instant - self._monotonic,
            "mode": "passive",
            "entire_run_observed": False,
            "external_control_unobserved": True,
        }
        attached = raw.get("attached")
        if type(attached) is bool:
            record["attached"] = attached
        partial = raw.get("partial")
        if type(partial) is bool:
            record["partial"] = partial
        state = raw.get("state")
        if isinstance(state, str) and state in {
            "unknown",
            "idle",
            "buffering",
            "playing",
            "paused",
            "ended",
            "stopped",
        }:
            record["state"] = state
        content = self._identifier("content", raw.get("requested_content_id"))
        if content is not None:
            record["requested_content_id"] = content
        device = raw.get("device")
        if isinstance(device, dict):
            record["device"] = {
                "uuid": self._identifier("device", device.get("uuid")) or "device-unknown",
                "name": None,
            }
        gap = _number(raw.get("gap_seconds"))
        if gap is not None:
            record["gap_seconds"] = gap
        gap_kind = raw.get("gap_kind")
        if gap_kind == "media":
            record["gap_kind"] = "media"
        elif gap_kind == "transport":
            record["gap_kind"] = "transport"
        reason = raw.get("stop_reason")
        reasons: dict[str, Literal["deadline", "interrupted", "backend_error", "cancelled"]] = {
            value: value for value in ("deadline", "interrupted", "backend_error", "cancelled")
        }
        if isinstance(reason, str) and reason in reasons:
            record["stop_reason"] = reasons[reason]
        observations = raw.get("observations")
        if isinstance(observations, list):
            record["observations"] = [
                self.observation(value) for value in observations if isinstance(value, dict)
            ]
        # Policy conclusions are intentionally not copied from an unvalidated file.
        # Replay the normalized observations to calculate them again.
        return record


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if isfinite(value) else None
    except OverflowError:
        return None


def export_capture(source: Path, destination: Path) -> None:
    """Stream a private journal to a sanitized fixture; reject truncated records.

    No exception embeds source text. Review the resulting fixture before committing.
    A crash-truncated final line must be explicitly removed from a copy, never
    silently interpreted as a successful completed capture.
    """
    sanitizer = CaptureSanitizer()
    with JsonlJournal(destination) as sink:
        for value in _read_records(source):
            sink.write(sanitizer.record(value))


def inspect_capture(source: Path) -> LifecycleInspection:
    """Count terminal candidates and their missing fields without carrying identity.

    This deliberately does not assign an anonymous terminal to preceding content,
    even if its media-session ID matches. Partial windows remain counted as
    diagnostics, visibly separate from any replay/acceptance decision.
    """
    sanitizer = CaptureSanitizer()
    requested: str | None = None
    result: LifecycleInspection = {
        "schema_version": 1,
        "provenance": "synthetic",
        "end_record_present": False,
        "stop_reason": None,
        "windows": 0,
        "partial_windows": 0,
        "media_observations": 0,
        "wire_diagnostic_observations": 0,
        "wire_fields": {},
        "terminal_candidates": 0,
        "partial_terminal_candidates": 0,
        "terminal_content": {"requested": 0, "other": 0, "unknown": 0},
        "terminal_ad": {"active": 0, "inactive": 0, "unknown": 0},
        "terminal_wire_diagnostics": 0,
        "terminal_wire_fields": {},
        "new_hardware_evidence": False,
    }
    for raw in _read_records(source):
        record = sanitizer.record(raw)
        if record["kind"] == "begin":
            requested = record.get("requested_content_id")
            if requested is None:
                raise ValueError("Inspection needs an explicit requested content.")
            result["provenance"] = (
                "synthetic" if record["provenance"] == "synthetic" else "sanitized-live"
            )
        if record["kind"] == "end":
            result["end_record_present"] = True
            result["stop_reason"] = record.get("stop_reason")
        if record["kind"] != "window":
            continue
        result["windows"] += 1
        result["partial_windows"] += int(record.get("partial", False))
        for event in record.get("observations", []):
            if event["kind"] != "media":
                continue
            result["media_observations"] += 1
            result["wire_diagnostic_observations"] += int("wire_status_count" in event)
            _count_wire_fields(event, result["wire_fields"])
            if event.get("player_state") != "IDLE" or event.get("idle_reason") != "FINISHED":
                continue
            result["terminal_candidates"] += 1
            result["partial_terminal_candidates"] += int(record.get("partial", False))
            content = event.get("content_id")
            result["terminal_content"][
                "unknown" if content is None else "requested" if content == requested else "other"
            ] += 1
            ad = event.get("ad_break")
            result["terminal_ad"]["unknown" if ad is None else "active" if ad else "inactive"] += 1
            result["terminal_wire_diagnostics"] += int("wire_status_count" in event)
            _count_wire_fields(event, result["terminal_wire_fields"])
    if requested is None:
        raise ValueError("Inspection needs a begin record.")
    return result


def _count_wire_fields(event: Observation, fields: dict[str, dict[WireFieldShape, int]]) -> None:
    for key in sorted(_WIRE_FIELDS):
        shape = event.get(key)
        if isinstance(shape, str) and shape in _WIRE_SHAPES:
            counts = fields.setdefault(key, {})
            value = _WIRE_SHAPES[shape]
            counts[value] = counts.get(value, 0) + 1


def _read_records(source: Path) -> Iterable[Mapping[str, object]]:
    with source.open(encoding="utf-8") as stream:
        while line := stream.readline(1_048_577):
            if len(line) > 1_048_576 or not line.endswith("\n"):
                raise ValueError("Lifecycle journal contains an oversized or incomplete record.")
            try:
                value: object = json.loads(line)
            except json.JSONDecodeError:
                raise ValueError("Lifecycle journal contains invalid JSON.") from None
            if not isinstance(value, dict):
                raise ValueError("Lifecycle journal records must be objects.")
            yield value


class _ReplayClock:
    def __init__(self, record: LifecycleRecord) -> None:
        self.record = record

    def monotonic(self) -> float:
        return self.record["monotonic"]

    def utcnow(self) -> datetime:
        return datetime.fromisoformat(self.record["recorded_at"])


class _ReplayTransport:
    def __init__(self) -> None:
        self.batch: list[Observation] = []

    def observe(self, seconds: float) -> list[Observation]:
        del seconds
        result, self.batch = self.batch, []
        return result

    def play(self, content_id: str) -> None:
        raise AssertionError("Replay must not play content.")

    def quit(self) -> None:
        raise AssertionError("Replay must not stop content.")


def replay_capture(source: Path, destination: Path) -> None:
    """Re-evaluate an exported or synthetic trace through Cast normalization/policy.

    This streams observations without sockets or wall-clock waiting. Input is
    sanitized again before evaluation; old evidence booleans are never trusted.
    A replay preserves source provenance and cannot become new hardware evidence.
    """
    sanitizer = CaptureSanitizer()
    transport = _ReplayTransport()
    tracker: LifecycleTracker | None = None
    backend: CastBackend | None = None
    clock: _ReplayClock | None = None
    with JsonlJournal(destination) as sink:
        for raw in _read_records(source):
            record = sanitizer.record(raw)
            if tracker is None:
                device = record.get("device")
                content_id = record.get("requested_content_id")
                if device is None or content_id is None:
                    raise ValueError("Replay needs an explicit target and content.")
                target = PlaybackTarget(device["uuid"], "cast")
                content = ContentRef("youtube", content_id)
                clock = _ReplayClock(record)
                backend = CastBackend(transport, target, clock, read_only=True)
                identity = record["capture_id"]
                tracker = LifecycleTracker(
                    PlaybackRequest(identity, identity, identity, content, target),
                    record["monotonic"],
                )
            assert clock is not None and backend is not None
            if record["monotonic"] < clock.monotonic():
                raise ValueError("Replay record times must not move backwards.")
            clock.record = record
            # Interrupted windows are diagnostic only, even if their queued tail
            # contains a terminal status. Replaying must preserve that boundary.
            if (record.get("partial") or record["kind"] == "gap") and tracker.snapshot is not None:
                tracker.snapshot = policy.invalidate_history(tracker.snapshot)
            transport.batch = [] if record.get("partial") else record.get("observations", [])
            events = backend.observe_window(tracker.request.target, 2)
            backend.drain_raw_observations()
            tracker.observe(events, now=clock.monotonic())
            record["state"] = tracker.snapshot.state.value if tracker.snapshot else "unknown"
            record["evidence"] = tracker.evidence()
            record["attached"] = tracker.snapshot is not None
            sink.write(record)
