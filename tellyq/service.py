"""Explicit foreground composition; construction and device work stay on the owner."""

import json
import re
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import cast
from uuid import UUID

from .clock import SystemClock
from .domain.ports import Clock, PlaybackBackend
from .domain.queue import QueueEntry
from .domain.values import ContentKind, ContentRef, PlaybackTarget
from .models import ServiceReport
from .playback_task import PlaybackTask
from .queue_store import SQLiteQueueStore
from .runner import Cancellation, RunnerPhase, RunnerTimeout, RunnerUnavailable, SessionRunner
from .runner_codec import wire_snapshot
from .runner_ipc import IPCUnavailable, RunnerIPCServer
from .session_store import JsonSessionStore
from .state import load_queue

type ServiceBackendFactory = Callable[
    [PlaybackTarget, Clock], AbstractContextManager[PlaybackBackend]
]


@dataclass(frozen=True, slots=True)
class QueueSpec:
    queue_id: str
    target: PlaybackTarget
    items: tuple[QueueEntry, ...]
    legacy_source: Path | None = None


def _object(
    value: object, required: set[str], optional: set[str] | frozenset[str] = frozenset()
) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or not required <= value.keys()
        or value.keys() - required - optional
    ):
        raise ValueError("Queue manifest fields do not match schema version 1.")
    return cast(dict[str, object], value)


def _text(value: object, limit: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("Manifest strings must be nonempty and bounded.")
    return value


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate manifest field.")
        result[key] = value
    return result


def load_manifest(path: Path) -> QueueSpec:
    """Read a bounded explicit YouTube queue, with no runtime or device side effects."""
    with path.open("rb") as stream:
        encoded = stream.read(65_537)
    if len(encoded) > 65_536:
        raise ValueError("Queue manifest exceeds 64 KiB.")
    raw = _object(
        json.loads(encoded, object_pairs_hook=_pairs),
        {"schema_version", "queue_id", "target", "items"},
    )
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise ValueError("Unsupported queue manifest version.")
    target = _object(raw["target"], {"device_id", "route"}, {"name"})
    if target["route"] != "cast":
        raise ValueError("This service currently supports the Cast route.")
    device_id = str(UUID(_text(target["device_id"])))
    name = _text(target["name"]) if target.get("name") is not None else None
    entries = raw["items"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 32:
        raise ValueError("A manifest requires between one and 32 items.")
    items: list[QueueEntry] = []
    for entry in entries:
        item = _object(entry, {"item_id", "provider", "content_id"}, {"kind", "title"})
        content_id = _text(item["content_id"])
        if item["provider"] != "youtube" or re.fullmatch(r"[A-Za-z0-9_-]{11}", content_id) is None:
            raise ValueError("Each item must contain an exact YouTube video ID.")
        kind = ContentKind(_text(item.get("kind", "video")))
        if kind != ContentKind.VIDEO:
            raise ValueError("The YouTube Cast adapter currently identifies video content only.")
        title = _text(item["title"]) if item.get("title") is not None else None
        items.append(
            QueueEntry(_text(item["item_id"]), ContentRef("youtube", content_id, kind, title))
        )
    if len({item.item_id for item in items}) != len(items):
        raise ValueError("Queue item identities must be unique.")
    return QueueSpec(_text(raw["queue_id"]), PlaybackTarget(device_id, "cast", name), tuple(items))


def legacy_spec(runtime: Path, queue_id: str) -> QueueSpec:
    """Explicit migration input; reading it does not rewrite legacy files."""
    legacy = load_queue(runtime)
    return QueueSpec(_text(queue_id), PlaybackTarget(legacy["device_id"], "cast"), (), runtime)


@contextmanager
def cast_backend(target: PlaybackTarget, clock: Clock) -> Iterator[PlaybackBackend]:
    from .cast_backend import open_backend

    with open_backend(target, clock, 30, retain_raw=False) as (backend, _device):
        yield backend


def make_runner(
    runtime: Path, spec: QueueSpec, *, backend_factory: ServiceBackendFactory = cast_backend
) -> SessionRunner:
    """No I/O until start acquires both owner locks and invokes the factory."""

    def task_factory(_cancellation: Cancellation) -> PlaybackTask:
        clock = SystemClock()
        store = SQLiteQueueStore(runtime)
        if spec.legacy_source is not None:
            queue = store.import_legacy(spec.legacy_source, queue_id=spec.queue_id)
        else:
            queue = store.load(spec.queue_id)
            if queue is None:
                queue = store.create(spec.queue_id, spec.target, spec.items)
            elif queue.target != spec.target or tuple(
                (item.item_id, item.content) for item in queue.items
            ) != tuple((item.item_id, item.content) for item in spec.items):
                raise ValueError("Existing queue identity describes different work.")
        if queue.target != spec.target:
            raise ValueError("Imported target changed; reopen the explicit source.")
        return PlaybackTask(
            lambda: backend_factory(spec.target, clock),
            store,
            JsonSessionStore(runtime, clock),
            clock,
            queue_id=spec.queue_id,
            target=spec.target,
        )

    return SessionRunner(runtime, spec.target, task_factory)


def run_foreground(
    runtime: Path,
    spec: QueueSpec,
    emit: Callable[[ServiceReport], None],
    *,
    backend_factory: ServiceBackendFactory = cast_backend,
    interrupted: Event | None = None,
) -> int:
    """Keep ownership and IPC alive until the non-daemon worker actually exits.

    Ctrl-C requests local shutdown, not a remote stop. A slow device call remains
    visible as stopping; repeated signals never release its locks from this thread.
    """
    wake = interrupted if interrupted is not None else Event()
    runner = make_runner(runtime, spec, backend_factory=backend_factory)
    server: RunnerIPCServer | None = None
    terminal = {RunnerPhase.STOPPED, RunnerPhase.FAILED}
    try:
        try:
            runner.start()
        except RunnerTimeout:
            emit(
                {
                    "schema_version": 1,
                    "event": "initializing",
                    "snapshot": wire_snapshot(runner.snapshot()),
                }
            )
        while runner.snapshot().phase not in terminal:
            current = runner.snapshot()
            if (
                server is None
                and current.phase == RunnerPhase.RUNNING
                and current.view.queue is not None
            ):
                server = RunnerIPCServer(runtime, runner)
                server.start()
                emit(
                    {
                        "schema_version": 1,
                        "event": "ready",
                        "snapshot": wire_snapshot(runner.snapshot()),
                    }
                )
            if wake.wait(0.1):
                break
    except KeyboardInterrupt:
        pass
    except RunnerUnavailable:
        if runner.snapshot().phase != RunnerPhase.FAILED:
            raise
    finally:
        reported = False
        reporting_error: Exception | None = None
        while True:
            try:
                runner.shutdown(timeout=1)
                break
            except RunnerTimeout, KeyboardInterrupt:
                if not reported:
                    try:
                        emit(
                            {
                                "schema_version": 1,
                                "event": "stopping",
                                "snapshot": wire_snapshot(runner.snapshot()),
                            }
                        )
                    except Exception as exc:
                        # Losing stdout must not strand non-daemon owner/IPC threads.
                        reporting_error = exc
                    reported = True
        if server is not None:
            # IPC shutdown requests may still be returning their final response.
            while True:
                try:
                    server.close(timeout=2)
                    break
                except IPCUnavailable, KeyboardInterrupt:
                    continue
        if reporting_error is not None:
            raise reporting_error
    snapshot = runner.snapshot()
    emit({"schema_version": 1, "event": "closed", "snapshot": wire_snapshot(snapshot)})
    return int(snapshot.phase == RunnerPhase.FAILED)
