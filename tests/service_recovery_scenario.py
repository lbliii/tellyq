"""Real process-loss boundaries with a synthetic receiver and AF_UNIX-only IPC."""

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from threading import Event, Thread
from unittest.mock import Mock, patch

from tellyq import __main__ as cli
from tellyq import service
from tellyq.domain.queue import ExecutionState, QueueEntry, QueueIntent
from tellyq.domain.values import ContentRef, PlaybackTarget
from tellyq.queue_store import SQLiteQueueStore
from tellyq.runner_ipc import RunnerIPCClient, endpoint_path
from tellyq.service_checkpoint import ServiceCheckpointOptions, run_service_checkpoint
from tests.playback_support import FakeClock
from tests.service_scenario import Backend, denied, local_socket, wait_until

CRASH = 73
TARGET = PlaybackTarget("00000000-0000-4000-8000-000000000001", "cast")
SPEC = service.QueueSpec(
    "synthetic-recovery",
    TARGET,
    tuple(
        QueueEntry(str(index), ContentRef("youtube", content))
        for index, content in enumerate(("f9F7yDjSdNA", "hgsIFyITvJE", "f9F7yDjSdNA"))
    ),
)


class RecordedBackend(Backend):
    def __init__(self, runtime, boundary):
        super().__init__(FakeClock())
        self.runtime, self.boundary = runtime, boundary

    def record(self, action):
        with (self.runtime / "effects.jsonl").open("a") as stream:
            stream.write(json.dumps({"action": action}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def start(self, request):
        self.record("start")
        return super().start(request)

    def stop(self, scope):
        self.record("stop")
        return super().stop(scope)

    def observe(self, target):
        if self.active and self.boundary == "cancellation":
            # Hold content in verified PLAYING until the explicit stop command.
            self.reads = 0
        events = super().observe(target)
        if self.active and self.boundary == "takeover" and self.reads > 1:
            return (
                self.event(
                    target,
                    session_active=True,
                    session_id="foreign-session",
                    application_id="foreign-app",
                ),
            )
        return events


def start_service(runtime, boundary):
    backend = RecordedBackend(runtime, boundary)
    outcomes = []
    ready = Event()

    def emit(event):
        if event["event"] == "ready":
            ready.set()

    @contextmanager
    def factory(_target, _clock):
        yield backend

    def serve():
        outcomes.append(service.run_foreground(runtime, SPEC, emit, backend_factory=factory))

    patch.object(service, "SystemClock", return_value=backend.clock).start()
    thread = Thread(target=serve)
    thread.start()
    client = RunnerIPCClient(runtime)
    assert ready.wait(10), "Service never became ready."
    wait_until(lambda: client.status()["snapshot"]["view"]["queue"] is not None)
    return thread, client, outcomes


def command(runtime, action, command_id=None):
    argv = ["--runtime", str(runtime), action]
    if command_id is not None:
        argv += ["--command-id", command_id]
    output = io.StringIO()
    with redirect_stdout(output):
        assert cli.main(argv) == 0
    return json.loads(output.getvalue())


def crash_process(runtime, boundary):
    method = {
        "before_dispatch": "prepare_start",
        "after_effect_before_ack": "record_outcome",
        "after_completion": "settle_attempt",
        "cancellation": "cancel_queue",
        "takeover": None,
    }[boundary]
    original = getattr(SQLiteQueueStore, method) if method else None

    def fail(self, *args, **kwargs):
        if boundary == "after_effect_before_ack":
            os._exit(CRASH)
        assert original is not None
        original(self, *args, **kwargs)
        os._exit(CRASH)

    if method is not None:
        patch.object(SQLiteQueueStore, method, fail).start()
    _thread, client, _outcomes = start_service(runtime, boundary)
    assert not (runtime / "effects.jsonl").exists()
    accepted = command(runtime, "start")
    if boundary == "cancellation":
        wait_until(
            lambda accepted=accepted: (
                client.ticket(accepted["ticket_id"])["ticket"]["state"] == "handled"
            )
        )
        command(runtime, "stop", "explicit-cancel")
    if boundary == "takeover":

        def taken_over():
            playback = client.status()["snapshot"]["view"]["playback"]
            return playback is not None and playback["ownership_lost"] is True

        wait_until(taken_over)
        assert effects(runtime) == ["start"]
        os._exit(CRASH)
    wait_until(lambda: False, timeout=10)


def effects(runtime):
    path = runtime / "effects.jsonl"
    return (
        [json.loads(line)["action"] for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


def run(boundary):
    with tempfile.TemporaryDirectory(prefix="tq-recovery-", dir="/tmp") as directory:
        runtime = Path(directory) / "runtime"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "tests.service_recovery_scenario",
                "crash",
                boundary,
                str(runtime),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == CRASH, result.stdout + result.stderr
        store = SQLiteQueueStore(runtime)
        before = store.load(SPEC.queue_id)
        assert before is not None
        expected = [] if boundary == "before_dispatch" else ["start"]
        assert effects(runtime) == expected
        assert len(before.attempts) == 1
        assert before.items[1].intent == QueueIntent.PENDING
        if boundary == "before_dispatch":
            assert before.commands[0].state == ExecutionState.PENDING
        elif boundary == "after_effect_before_ack":
            assert before.commands[0].state == ExecutionState.DISPATCHED
        elif boundary == "after_completion":
            assert before.items[0].intent == QueueIntent.FINISHED
        elif boundary == "cancellation":
            assert before.cancellation_requested
        else:
            assert before.items[0].intent == QueueIntent.CURRENT
        thread, client, outcomes = start_service(runtime, "restart")
        try:
            queue = client.status()["snapshot"]["view"]["queue"]
            assert queue is not None
            assert effects(runtime) == expected  # Merely opening the owner never plays.
            for command_id in (None, "new-command-after-restart"):
                accepted = command(runtime, "start", command_id)
                wait_until(
                    lambda accepted=accepted: (
                        client.ticket(accepted["ticket_id"])["ticket"]["state"] == "handled"
                    )
                )
                assert effects(runtime) == expected
            recovered = store.load(SPEC.queue_id)
            assert recovered is not None
            assert len(recovered.attempts) == 1
            assert recovered.items[1].intent == QueueIntent.PENDING
            assert recovered.cancellation_requested
            if boundary == "after_completion":
                assert recovered.items[0].intent == QueueIntent.FINISHED
            if boundary == "after_effect_before_ack":
                assert recovered.commands[0].state == ExecutionState.UNCERTAIN
        finally:
            client.shutdown()
            thread.join(2)
        assert not thread.is_alive() and outcomes == [0]
        assert not endpoint_path(runtime).exists()
        assert effects(runtime) == expected
        return {
            "boundary": boundary,
            "starts": len(expected),
            "replayed_effects": 0,
            "successor_starts": 0,
        }


def checkpoint():
    with tempfile.TemporaryDirectory(prefix="tq-checkpoint-", dir="/tmp") as directory:
        runtime = Path(directory) / "runtime"
        thread, client, outcomes = start_service(runtime, "natural")
        try:
            summary = run_service_checkpoint(
                client,
                SPEC,
                ServiceCheckpointOptions("start", seconds=5, poll_seconds=0.05, cleanup_stop=True),
                Mock(),
            )
            assert effects(runtime) == ["start", "stop"] * 3
            assert summary["stop_reason"] == "queue_finished_and_released"
            assert summary["cleanup"] == "release_observed"
            assert summary["verified_handoffs"] == 2
            assert all(item["verified_at_ms"] is not None for item in summary["items"])
            assert all(item["finished_at_ms"] is not None for item in summary["items"])
            assert summary["commands"][0]["first_verified_state_ms"] is not None
            return summary
        finally:
            client.shutdown()
            thread.join(2)
            assert not thread.is_alive() and outcomes == [0]


if __name__ == "__main__":
    patch.object(socket, "socket", local_socket).start()
    patch.object(socket, "getaddrinfo", denied).start()
    if sys.argv[1] == "crash":
        crash_process(Path(sys.argv[3]), sys.argv[2])
    elif sys.argv[1] == "checkpoint":
        sys.stdout.write(json.dumps(checkpoint()))
    else:
        sys.stdout.write(json.dumps(run(sys.argv[1])))
