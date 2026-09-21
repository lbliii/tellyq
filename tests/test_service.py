"""Foreground configuration and ownership tests never open a network connection."""

import json
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event, get_ident

import pytest

from tellyq import service
from tellyq.domain.queue import QueueIntent
from tellyq.domain.values import PlaybackCapabilities, PlaybackTarget
from tellyq.queue_store import SQLiteQueueStore
from tellyq.runner import RunnerPhase, RunnerSnapshot, RunnerTimeout, RunnerUnavailable, TaskView
from tellyq.state import make_queue, save


def manifest():
    return {
        "schema_version": 1,
        "queue_id": "session-a",
        "target": {"device_id": "00000000-0000-4000-8000-000000000001", "route": "cast"},
        "items": [{"item_id": "first", "provider": "youtube", "content_id": "FozIp7Va7dY"}],
    }


def load(tmp_path, value):
    path = tmp_path / "input.json"
    path.write_text(json.dumps(value))
    return service.load_manifest(path)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(schema_version=True),
        lambda value: value.update(schema_version=2),
        lambda value: value.update(extra="unknown"),
        lambda value: value.update(items=[]),
        lambda value: value.update(items=value["items"] * 2),
        lambda value: value["target"].update(device_id="name"),
        lambda value: value["target"].update(route="other"),
        lambda value: value["items"][0].update(provider="netflix"),
        lambda value: value["items"][0].update(
            content_id="https://youtube.com/watch?v=FozIp7Va7dY"
        ),
        lambda value: value["items"][0].update(kind="unknown"),
        lambda value: value["items"][0].update(kind="episode"),
    ],
)
def test_invalid_manifest_is_inert(tmp_path, mutate):
    value = manifest()
    mutate(value)
    with pytest.raises(ValueError):
        load(tmp_path, value)
    assert {path.name for path in tmp_path.iterdir()} == {"input.json"}


def test_duplicate_keys_and_oversize_manifest_rejected(tmp_path):
    path = tmp_path / "input.json"
    path.write_text('{"schema_version":1,"schema_version":1}')
    with pytest.raises(ValueError, match="Duplicate"):
        service.load_manifest(path)
    path.write_bytes(b" " * 65_537)
    with pytest.raises(ValueError, match="64 KiB"):
        service.load_manifest(path)


def test_foreground_creation_is_inert_and_backend_lifetime_on_owner(tmp_path):
    calls = []

    class Backend:
        def capabilities(self, target):
            return PlaybackCapabilities()

        def observe(self, target):
            calls.append(("observe", get_ident()))
            return ()

        def start(self, request):
            pytest.fail("serve must not start playback")

        def stop(self, scope):
            pytest.fail("shutdown must not stop playback")

    @contextmanager
    def backend(target, clock):
        calls.append(("enter", get_ident()))
        try:
            yield Backend()
        finally:
            calls.append(("close", get_ident()))

    runtime = tmp_path / "runtime"
    spec = load(tmp_path, manifest())
    runner = service.make_runner(runtime, spec, backend_factory=backend)
    assert not runtime.exists() and not calls
    runner.start()
    try:
        second = service.make_runner(runtime, spec, backend_factory=backend)
        with pytest.raises(RunnerUnavailable):
            second.start()
        second.shutdown()
    finally:
        runner.shutdown()
    assert [name for name, _ in calls].count("enter") == 1
    assert [name for name, _ in calls].count("close") == 1
    assert len({thread for _, thread in calls}) == 1
    assert calls[0][1] != get_ident()
    saved = SQLiteQueueStore(runtime).load(spec.queue_id)
    assert saved and saved.items[0].intent == QueueIntent.PENDING
    assert saved.attempts == ()


def test_existing_queue_identity_cannot_be_repurposed(tmp_path):
    spec = load(tmp_path, manifest())
    runtime = tmp_path / "runtime"
    store = SQLiteQueueStore(runtime)
    store.create(spec.queue_id, spec.target, spec.items)
    changed = replace(spec, target=PlaybackTarget("00000000-0000-4000-8000-000000000002", "cast"))

    def forbidden(*args):
        pytest.fail("invalid queue must fail before backend construction")

    runner = service.make_runner(runtime, changed, backend_factory=forbidden)
    with pytest.raises(RunnerUnavailable):
        runner.start()
    runner.shutdown()
    saved = store.load(spec.queue_id)
    assert saved is not None and saved.target == spec.target


def test_legacy_import_is_explicit_and_preserves_source(tmp_path):
    save(tmp_path / "queue.json", make_queue("00000000-0000-4000-8000-000000000001"))
    before = (tmp_path / "queue.json").read_bytes()
    spec = service.legacy_spec(tmp_path, "import-a")
    assert not (tmp_path / "queue.sqlite3").exists()
    assert spec.legacy_source == tmp_path
    assert (tmp_path / "queue.json").read_bytes() == before


def test_startup_failure_reports_cached_category_after_cleanup(tmp_path):
    spec = load(tmp_path, manifest())
    events = []

    def failed_backend(target, clock):
        raise RuntimeError("private receiver address must not escape")

    assert (
        service.run_foreground(
            tmp_path / "runtime", spec, events.append, backend_factory=failed_backend
        )
        == 1
    )
    assert events[-1]["event"] == "closed"
    assert events[-1]["snapshot"]["failure"] == "task_initialization_failed"
    assert events[-1]["snapshot"]["owns_device"] is False
    assert "private receiver" not in json.dumps(events)


def test_cli_ipc_playback_queue_end_to_end():
    # A new interpreter permits AF_UNIX only, keeping pytest's blanket socket/DNS
    # guard intact. The scenario composes CLI + IPC + real task/SQLite with a fake TV.
    result = subprocess.run(
        [sys.executable, "-m", "tests.service_scenario"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=8,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report == {
        "starts": 3,
        "releases": 3,
        "finished": 3,
        "single_owner": True,
        "recovery_held": True,
    }


def test_reporting_failure_does_not_abandon_shutdown(tmp_path, monkeypatch):
    spec = load(tmp_path, manifest())
    store = SQLiteQueueStore(tmp_path / "state")
    queue = store.create(spec.queue_id, spec.target, spec.items)
    interrupted = Event()
    interrupted.set()
    calls = []

    class Runner:
        closed = False

        def start(self):
            pass

        def snapshot(self):
            return RunnerSnapshot(
                phase=RunnerPhase.STOPPED if self.closed else RunnerPhase.RUNNING,
                owns_device=not self.closed,
                view=TaskView(queue=queue),
            )

        def shutdown(self, timeout):
            calls.append("shutdown")
            if calls.count("shutdown") == 1:
                raise RunnerTimeout("busy")
            self.closed = True
            return self.snapshot()

    class Server:
        def __init__(self, runtime, runner):
            self.runner = runner

        def start(self):
            calls.append("ipc-start")

        def close(self, timeout):
            assert self.runner.closed
            calls.append("ipc-close")

    def emit(report):
        if report["event"] == "stopping":
            raise BrokenPipeError("stdout closed")

    monkeypatch.setattr(service, "make_runner", lambda *args, **kwargs: Runner())
    monkeypatch.setattr(service, "RunnerIPCServer", Server)
    with pytest.raises(BrokenPipeError):
        service.run_foreground(tmp_path, spec, emit, interrupted=interrupted)
    assert calls == ["ipc-start", "shutdown", "shutdown", "ipc-close"]
