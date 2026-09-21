"""Local state stays in runtime/. Atomic writes and a process lock protect commands."""

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from .cast import VIDEO_ID, now
from .models import QueueDocument

RUNTIME = Path.cwd() / "runtime"


def save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4()}.tmp")
    try:
        with open(temporary, "x", opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
            stream.write(json.dumps(value, indent=2) + "\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read(path: Path) -> dict[str, Any] | None:
    """Read a JSON object at the untyped persistence boundary."""
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path.name}.")
    return value


@contextmanager
def command_lock(runtime: Path) -> Iterator[None]:
    runtime.mkdir(parents=True, exist_ok=True)
    with (runtime / "command.lock").open("a") as lock:
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
        "updated_at": now(),
        "items": [
            {
                "id": "autumn-fantasy",
                "service": "youtube",
                "content_id": VIDEO_ID,
                "title": "Bob Ross — Autumn Fantasy (Season 20 Episode 7)",
                "url": f"https://www.youtube.com/watch?v={VIDEO_ID}",
                "state": "queued",
            }
        ],
    }


def load_queue(runtime: Path) -> QueueDocument:
    queue = read(runtime / "queue.json")
    if (
        not queue
        or queue.get("schema_version") != 1
        or len(queue.get("items", [])) != 1
        or queue["items"][0].get("content_id") != VIDEO_ID
        or queue["items"][0].get("service") != "youtube"
    ):
        raise ValueError("Queue one Bob Ross episode with `queue --device UUID` first.")
    UUID(queue["device_id"])
    return cast(QueueDocument, queue)
