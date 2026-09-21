"""Local state stays in runtime/. Atomic writes and a process lock protect commands."""

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from math import isfinite
from pathlib import Path
from typing import Any, Never
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

from .clock import SystemClock
from .models import QueueDocument, QueueItem
from .programs import (
    DEFAULT_CONTENT_ID,
    DEFAULT_ITEM_ID,
    DEFAULT_SERVICE,
    DEFAULT_TITLE,
    DEFAULT_URL,
)

RUNTIME = Path.cwd() / "runtime"
_CLOCK = SystemClock()
_QUEUE_STATES = frozenset(
    {"queued", "starting", "playing", "failed", "unconfirmed", "stopped", "finished"}
)


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object.")
    document: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError(f"{label} must have string field names.")
        document[key] = item
    return document


def _string(document: dict[str, object], field: str, label: str) -> str:
    value = document.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}.{field} must be a nonempty string.")
    return value


def _device_id(document: dict[str, object], label: str) -> str:
    value = _string(document, "device_id", label)
    try:
        UUID(value)
    except ValueError:
        raise ValueError(f"{label}.device_id must be a UUID.") from None
    return value


def _version(document: dict[str, object], label: str) -> None:
    value = document.get("schema_version")
    if type(value) is not int:
        raise ValueError(f"{label}.schema_version must be the integer 1.")
    if value != 1:
        raise ValueError(f"Unsupported schema version in {label}; only version 1 is supported.")


def _known_fields(document: dict[str, object], fields: set[str], label: str) -> None:
    if document.keys() - fields:
        raise ValueError(
            f"{label} contains unsupported fields; keep the existing state for recovery."
        )


def _queue_document(value: object) -> QueueDocument:
    """Validate and construct the existing wire contract without asserting an unchecked type."""
    label = "queue.json"
    document = _object(value, label)
    _version(document, label)
    _known_fields(
        document, {"schema_version", "device_id", "updated_at", "items", "last_report"}, label
    )
    device_id = _device_id(document, label)
    updated_at = _string(document, "updated_at", label)
    try:
        timestamp = datetime.fromisoformat(updated_at)
    except ValueError:
        raise ValueError(
            "queue.json.updated_at must be an ISO 8601 timestamp with a timezone."
        ) from None
    if timestamp.tzinfo is None:
        raise ValueError("queue.json.updated_at must be an ISO 8601 timestamp with a timezone.")
    items = document.get("items")
    if not isinstance(items, list) or len(items) != 1:
        raise ValueError("queue.json.items must contain exactly one Bob Ross program.")
    item_label = "queue.json.items[0]"
    item = _object(items[0], item_label)
    _known_fields(item, {"id", "service", "content_id", "title", "url", "state"}, item_label)
    item_id = _string(item, "id", item_label)
    service = _string(item, "service", item_label)
    content_id = _string(item, "content_id", item_label)
    if service != DEFAULT_SERVICE or content_id != DEFAULT_CONTENT_ID:
        raise ValueError(
            "queue.json.items[0] must identify the supported Bob Ross YouTube program."
        )
    title = _string(item, "title", item_label)
    url = _string(item, "url", item_label)
    try:
        parsed_url = urlsplit(url)
    except ValueError:
        raise ValueError(
            "queue.json.items[0].url must be a YouTube watch URL for this program."
        ) from None
    if (
        parsed_url.scheme != "https"
        or parsed_url.netloc not in {"youtube.com", "www.youtube.com"}
        or parsed_url.path != "/watch"
        or parse_qs(parsed_url.query).get("v") != [content_id]
    ):
        raise ValueError("queue.json.items[0].url must be a YouTube watch URL for this program.")
    state = _string(item, "state", item_label)
    if state not in _QUEUE_STATES:
        raise ValueError("queue.json.items[0].state is not a supported queue state.")
    validated_item: QueueItem = {
        "id": item_id,
        "service": service,
        "content_id": content_id,
        "title": title,
        "url": url,
        "state": state,
    }
    queue: QueueDocument = {
        "schema_version": 1,
        "device_id": device_id,
        "updated_at": updated_at,
        "items": [validated_item],
    }
    if "last_report" in document:
        queue["last_report"] = _string(document, "last_report", label)
    return queue


def _validate_session(value: object) -> None:
    label = "session.json"
    document = _object(value, label)
    # Original session files are unversioned; an explicit v1 uses the same fields.
    if "schema_version" in document:
        _version(document, label)
    _known_fields(
        document, {"schema_version", "device_id", "content_id", "app_id", "app_session_id"}, label
    )
    _device_id(document, label)
    if _string(document, "content_id", label) != DEFAULT_CONTENT_ID:
        raise ValueError("session.json.content_id must identify the supported Bob Ross program.")
    _string(document, "app_id", label)
    _string(document, "app_session_id", label)


def _validate_state(path: Path, value: object) -> None:
    if path.name == "queue.json":
        _queue_document(value)
    elif path.name == "session.json":
        _validate_session(value)


def _invalid_constant(_value: str) -> Never:
    raise ValueError("Non-finite numbers are not valid JSON.")


def _finite_float(value: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError("Non-finite numbers are not valid JSON.")
    return number


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate object fields are not supported in JSON state.")
        value[key] = item
    return value


def save(path: Path, value: object) -> None:
    """Validate named state and atomically replace it with a private JSON file."""
    _validate_state(path, value)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4()}.tmp")
    try:
        with open(
            temporary, "x", encoding="utf-8", opener=lambda name, flags: os.open(name, flags, 0o600)
        ) as stream:
            stream.write(json.dumps(value, indent=2, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read(path: Path) -> dict[str, Any] | None:
    """Read JSON; queue.json/session.json receive their full existing schema checks.

    Reports and other JSON objects remain generic. Missing state is distinct from
    malformed state, which must fail before a caller can select a device.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeError:
        raise ValueError(f"{path.name} must contain UTF-8 JSON.") from None
    try:
        value = json.loads(
            source,
            parse_constant=_invalid_constant,
            parse_float=_finite_float,
            object_pairs_hook=_unique_object,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid JSON in {path.name} at line {exc.lineno}, column {exc.colno}."
        ) from exc
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path.name}.")
    _validate_state(path, value)
    return value


@contextmanager
def command_lock(runtime: Path) -> Iterator[None]:
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(
        runtime / "command.lock", "a", opener=lambda name, flags: os.open(name, flags, 0o600)
    ) as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another TellyQ command is running; wait for it to finish.") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def make_queue(device_id: str) -> QueueDocument:
    return {
        "schema_version": 1,
        "device_id": str(UUID(device_id)),
        "updated_at": _CLOCK.utcnow().isoformat(),
        "items": [
            {
                "id": DEFAULT_ITEM_ID,
                "service": DEFAULT_SERVICE,
                "content_id": DEFAULT_CONTENT_ID,
                "title": DEFAULT_TITLE,
                "url": DEFAULT_URL,
                "state": "queued",
            }
        ],
    }


def load_queue(runtime: Path) -> QueueDocument:
    queue = read(runtime / "queue.json")
    if queue is None:
        raise ValueError("Queue one Bob Ross episode with `queue --device UUID` first.")
    return _queue_document(queue)
