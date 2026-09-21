"""Opt-in structural inspection of current-message YouTube Cast custom data.

The ordinary projection emits neither arbitrary names nor arbitrary scalar values.
An explicitly requested private inventory retains bounded, filtered field names only.
Neither projection changes playback evidence or attributes anonymous terminal messages.
"""

import json
import os
from collections import deque
from collections.abc import Iterator
from itertools import islice
from math import isfinite
from pathlib import Path
from re import fullmatch, search
from threading import Lock
from types import TracebackType
from typing import Literal, TypeGuard

from .cast_backend import video_id
from .cast_messages import custom_player_state, media_observation
from .models import (
    MetadataStructure,
    MetadataValueType,
    PrivateMetadataField,
    PrivateMetadataSchema,
    YouTubeMetadataRecord,
    YouTubeMetadataSample,
)

_MAX_NODES = 128
_MAX_DEPTH = 6
_MAX_ENTRIES = 10_000
_MAX_PENDING = 256
type ContainerName = Literal["status", "media", "extended_media"]


def _object(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _field(parent: object, name: str) -> object:
    return parent.get(name) if _object(parent) else None


def _kind(value: object) -> MetadataValueType:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, int | float):
        try:
            return "number" if isfinite(value) else "invalid"
        except OverflowError:
            return "invalid"
    return "invalid"


def _entries(value: object) -> int | None:
    return min(len(value), _MAX_ENTRIES) if isinstance(value, dict | list) else None


def _private_name(key: object) -> str:
    if (
        not isinstance(key, str)
        or fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,47}", key) is None
        or search(r"token|auth|cookie|password|secret|email|account|device|session", key, flags=2)
    ):
        return "[redacted-key]"
    return key


def _children(value: object, budget: int) -> Iterator[tuple[object, object]]:
    if _object(value):
        yield from islice(value.items(), budget)
    elif isinstance(value, list):
        yield from (("[]", item) for item in islice(value, budget))


def _structure(
    parent: object, *, private_schema: bool
) -> tuple[MetadataStructure, list[PrivateMetadataField]]:
    summary: MetadataStructure = {
        "shape": "unavailable",
        "entries": None,
        "inspected_nodes": 0,
        "maximum_depth": 0,
        "truncated": False,
        "types": {
            "object": 0,
            "array": 0,
            "string": 0,
            "boolean": 0,
            "number": 0,
            "null": 0,
            "invalid": 0,
        },
    }
    fields: list[PrivateMetadataField] = []
    if not _object(parent):
        return summary, fields
    if "customData" not in parent:
        summary["shape"] = "absent"
        return summary, fields
    root = parent["customData"]
    summary["shape"] = "null" if root is None else "valid" if _object(root) else "invalid"
    summary["entries"] = _entries(root)
    pending: list[tuple[object, tuple[str, ...]]] = [(root, ())]
    visited: set[int] = set()
    while pending and summary["inspected_nodes"] < _MAX_NODES:
        value, path = pending.pop()
        depth = len(path)
        summary["inspected_nodes"] += 1
        summary["maximum_depth"] = max(summary["maximum_depth"], depth)
        summary["types"][_kind(value)] += 1
        if private_schema:
            fields.append(
                {"path": list(path), "value_type": _kind(value), "entries": _entries(value)}
            )
        if not isinstance(value, dict | list):
            continue
        if id(value) in visited:
            summary["truncated"] = True
            continue
        visited.add(id(value))
        remaining = _MAX_NODES - summary["inspected_nodes"] - len(pending)
        if depth == _MAX_DEPTH:
            summary["truncated"] |= bool(value)
            continue
        summary["truncated"] |= len(value) > remaining
        for key, child in _children(value, max(0, remaining)):
            component = "[]" if isinstance(value, list) else _private_name(key)
            pending.append((child, (*path, component)))
    summary["truncated"] |= bool(pending)
    return summary, fields


def _containers(data: object) -> dict[ContainerName, object]:
    statuses = _field(data, "status")
    status = statuses[0] if isinstance(statuses, list) and statuses else None
    return {
        "status": status,
        "media": _field(status, "media"),
        "extended_media": _field(_field(status, "extendedStatus"), "media"),
    }


def project_metadata(
    data: object,
    *,
    requested_content: str,
    sequence: int,
    observed_at: str,
    monotonic: float,
    previous_media_session: int | None = None,
    private_schema: bool = False,
) -> tuple[YouTubeMetadataSample, PrivateMetadataSchema | None]:
    """Project one raw message without mutating it or carrying prior media fields forward."""
    normalized = media_observation(data)
    identifier = video_id(normalized.get("content_id"))
    current_session = normalized.get("media_session_id")
    statuses = _field(data, "status")
    containers = _containers(data)
    provider_state, provider_shape = custom_player_state(containers["status"])
    sample: YouTubeMetadataSample = {
        "sequence": sequence,
        "observed_at": observed_at,
        "monotonic": monotonic,
        "status_count": len(statuses) if isinstance(statuses, list) else None,
        "first_status_only": True,
        "content_relation": "unknown"
        if identifier is None
        else "requested"
        if identifier == requested_content
        else "other",
        "media_session_relation": "unknown"
        if current_session is None
        else "first"
        if previous_media_session is None
        else "same"
        if current_session == previous_media_session
        else "changed",
        "player_state": normalized.get("player_state"),
        "custom_player_state": provider_state,
        "custom_player_state_shape": provider_shape,
        "idle_reason": normalized.get("idle_reason"),
        "position": normalized.get("position"),
        "duration": normalized.get("duration"),
        "ad_break": normalized.get("ad_break"),
        "custom_data": {},
    }
    schema: PrivateMetadataSchema = {"sequence": sequence, "fields": {}, "truncated": False}
    for name, parent in containers.items():
        structure, fields = _structure(parent, private_schema=private_schema)
        sample["custom_data"][name] = structure
        if private_schema:
            schema["fields"][name] = fields
            schema["truncated"] |= structure["truncated"]
    return sample, schema if private_schema else None


class YouTubeMetadataProbe:
    """Bounded callback-to-caller transfer, retaining projections rather than raw messages."""

    def __init__(self, requested_content: str, *, private_schema: bool = False) -> None:
        if fullmatch(r"[A-Za-z0-9_-]{11}", requested_content) is None:
            raise ValueError("Content must be an exact 11-character YouTube video ID.")
        self.requested_content = requested_content
        self.private_schema = private_schema
        self._lock = Lock()
        self._pending: deque[tuple[YouTubeMetadataSample, PrivateMetadataSchema | None]] = deque()
        self._previous_media_session: int | None = None
        self._sequence = 0
        self._dropped = 0

    def receive(self, data: object, *, observed_at: str, monotonic: float) -> None:
        if _field(data, "type") != "MEDIA_STATUS":
            return
        with self._lock:
            self._sequence += 1
            sample, schema = project_metadata(
                data,
                requested_content=self.requested_content,
                sequence=self._sequence,
                observed_at=observed_at,
                monotonic=monotonic,
                previous_media_session=self._previous_media_session,
                private_schema=self.private_schema,
            )
            # Only the immediately preceding media message participates in equality.
            # Missing identity clears comparison; equality never establishes ownership.
            self._previous_media_session = media_observation(data).get("media_session_id")
            if len(self._pending) == _MAX_PENDING:
                self._pending.popleft()
                self._dropped += 1
            self._pending.append((sample, schema))

    def drain(self) -> tuple[list[tuple[YouTubeMetadataSample, PrivateMetadataSchema | None]], int]:
        with self._lock:
            result = list(self._pending)
            self._pending.clear()
            return result, self._dropped


class MetadataJournal:
    """Exclusive private journal; no arbitrary exception text or automatic public export."""

    def __init__(self, path: Path) -> None:
        if not path.resolve().is_relative_to(Path("runtime").resolve()):
            raise ValueError("Metadata journals must be under runtime/.")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._file = os.fdopen(fd, "w", encoding="utf-8")

    def write(self, record: YouTubeMetadataRecord) -> None:
        self._file.write(json.dumps(record, allow_nan=False, separators=(",", ":")) + "\n")
        self._file.flush()
        os.fsync(self._file.fileno())

    def __enter__(self) -> MetadataJournal:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._file.close()
