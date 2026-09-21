"""Revision-checked private JSON snapshots, with explicit restart reconciliation."""

from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from threading import RLock
from uuid import uuid4

from .domain.ports import Clock, RevisionConflict
from .domain.values import (
    ContentKind,
    ContentRef,
    PlaybackRequest,
    PlaybackScope,
    PlaybackTarget,
    PlayerState,
    SessionSnapshot,
)
from .models import SnapshotRecord
from .state import command_lock, read, save


def encode_snapshot(snapshot: SessionSnapshot) -> SnapshotRecord:
    scope = snapshot.scope
    request = scope.request
    return {
        "schema_version": 1,
        "revision": snapshot.revision,
        "request_id": request.request_id,
        "attempt_id": request.attempt_id,
        "queue_item_id": request.queue_item_id,
        "provider": request.content.provider,
        "content_id": request.content.content_id,
        "content_kind": request.content.kind.value,
        "title": request.content.title,
        "device_id": request.target.device_id,
        "route": request.target.route,
        "name": request.target.name,
        "session_id": scope.session_id,
        "application_id": scope.application_id,
        "historical_state": snapshot.state.value,
        "stop_requested": snapshot.stop_requested,
    }


def decode_snapshot(value: Mapping[str, object], clock: Clock) -> SessionSnapshot:
    fields = set(SnapshotRecord.__annotations__)
    if (
        set(value) != fields
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
    ):
        raise ValueError("Unsupported or malformed playback snapshot; preserve it for recovery.")
    revision = value["revision"]
    stopped = value["stop_requested"]
    if type(revision) is not int or revision < 0 or type(stopped) is not bool:
        raise ValueError("Invalid snapshot revision or stop request.")

    def string(key: str) -> str:
        item = value[key]
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"Snapshot {key} must be a nonempty string.")
        return item

    def optional(key: str) -> str | None:
        return None if value[key] is None else string(key)

    PlayerState(string("historical_state"))
    request = PlaybackRequest(
        string("request_id"),
        string("attempt_id"),
        string("queue_item_id"),
        ContentRef(
            string("provider"),
            string("content_id"),
            ContentKind(string("content_kind")),
            optional("title"),
        ),
        PlaybackTarget(string("device_id"), string("route"), optional("name")),
    )
    # A disk record never restores latest/anchor/receipt or any monotonic instant.
    # The fresh generation cannot consume callbacks from the previous connection.
    scope = PlaybackScope(
        request, string("session_id"), str(uuid4()), clock.monotonic(), optional("application_id")
    )
    return SessionSnapshot(scope, revision=revision, stop_requested=stopped)


class JsonSessionStore:
    def __init__(self, runtime: Path, clock: Clock) -> None:
        self.directory = runtime / "sessions"
        self.clock = clock
        self._cache: dict[str, SessionSnapshot] = {}
        self._mutex = RLock()

    def _path(self, attempt_id: str) -> Path:
        # Opaque IDs cannot escape runtime/ through path traversal.
        return self.directory / f"{sha256(attempt_id.encode()).hexdigest()}.json"

    def load(self, attempt_id: str) -> SessionSnapshot | None:
        with self._mutex:
            value = read(self._path(attempt_id))
            if value is None:
                return None
            recovered = decode_snapshot(value, self.clock)
            if recovered.scope.request.attempt_id != attempt_id:
                raise ValueError("Snapshot identity does not match its record.")
            cached = self._cache.get(attempt_id)
            if cached is not None and encode_snapshot(cached) == value:
                return cached
            return recovered

    def current(self) -> SessionSnapshot | None:
        pointer = read(self.directory / "active.json")
        if pointer is None:
            return None
        if (
            set(pointer) != {"schema_version", "attempt_id"}
            or type(pointer["schema_version"]) is not int
            or pointer["schema_version"] != 1
            or not isinstance(pointer["attempt_id"], str)
            or not pointer["attempt_id"]
        ):
            raise ValueError("Invalid active playback reference; preserve it for recovery.")
        result = self.load(pointer["attempt_id"])
        if result is None:
            raise ValueError("Active playback snapshot is missing; preserve it for recovery.")
        return result

    def save(self, snapshot: SessionSnapshot, *, expected_revision: int | None) -> None:
        with self._mutex, command_lock(self.directory):
            previous = self.load(snapshot.scope.request.attempt_id)
            actual = previous.revision if previous is not None else None
            if actual != expected_revision or (actual is not None and snapshot.revision <= actual):
                raise RevisionConflict("Playback snapshot changed; reload before retrying.")
            record = encode_snapshot(snapshot)
            decode_snapshot(record, self.clock)
            save(self._path(snapshot.scope.request.attempt_id), record)
            save(
                self.directory / "active.json",
                {
                    "schema_version": 1,
                    "attempt_id": snapshot.scope.request.attempt_id,
                },
            )
            self._cache[snapshot.scope.request.attempt_id] = snapshot
