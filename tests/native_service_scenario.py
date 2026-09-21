"""Composed native service checkpoint with synthetic playback and local-only IPC."""

import json
import socket
import tempfile
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

from tellyq import service
from tellyq.domain.queue import QueueMode
from tellyq.runner_ipc import RunnerIPCClient
from tellyq.service_checkpoint import ServiceCheckpointOptions, run_service_checkpoint
from tests.playback_support import FakeClock
from tests.service_recovery_scenario import command
from tests.service_scenario import denied, local_socket
from tests.test_native_task import ITEMS, TARGET, NativeBackend, actions


class Clock(FakeClock):
    def utcnow(self):
        return super().utcnow() + timedelta(seconds=self.instant)


class Backend(NativeBackend):
    def __init__(self, seen):
        super().__init__(Clock())
        self.seen, self.index = seen, 0

    def observe(self, target):
        if self.active and self.seen[self.index].is_set():
            if self.staged is not None:
                self.transition()
                self.index += 1
            elif self.index == 2:
                self.finish()
        return super().observe(target)


class Journal:
    def __init__(self, seen):
        self.seen = seen

    def write(self, kind, **fields):
        if kind != "status":
            return
        view = fields["response"]["snapshot"]["view"]
        playback, queue = view["playback"], view["queue"]
        if playback is None or queue is None:
            return
        if playback["evidence"]["receiver_playback_confirmed"] and not playback["ownership_lost"]:
            index = next(
                i for i, item in enumerate(ITEMS) if item.item_id == queue["current_item_id"]
            )
            self.seen[index].set()


def run():
    with tempfile.TemporaryDirectory(prefix="tq-native-", dir="/tmp") as directory:
        runtime = Path(directory) / "runtime"
        seen = [Event() for _ in ITEMS]
        backend, ready, outcomes = Backend(seen), Event(), []
        spec = service.QueueSpec("native-checkpoint", TARGET, ITEMS, mode=QueueMode.NATIVE)

        @contextmanager
        def factory(_target, _clock):
            yield backend

        def emit(event):
            if event["event"] == "ready":
                ready.set()

        def serve():
            outcomes.append(service.run_foreground(runtime, spec, emit, backend_factory=factory))

        with patch.object(service, "SystemClock", return_value=backend.clock):
            thread = Thread(target=serve)
            thread.start()
            client = RunnerIPCClient(runtime)
            try:
                assert ready.wait(5)
                assert actions(backend) == []
                # Exercise actual CLI routing before the checkpoint client uses IPC.
                assert command(runtime, "status")["snapshot"]["view"]["queue"]["mode"] == "native"
                summary = run_service_checkpoint(
                    client,
                    spec,
                    ServiceCheckpointOptions(
                        "start", seconds=8, poll_seconds=0.05, cleanup_stop=True
                    ),
                    Journal(seen),
                )
                assert summary["stop_reason"] == "queue_finished", summary
                assert summary["verified_handoffs"] == 2, summary
                assert summary["cleanup"] == "stop_observed", summary
                assert actions(backend) == ["start", "play_next", "play_next", "stop"]
                return summary
            finally:
                try:
                    if thread.is_alive():
                        client.shutdown()
                finally:
                    thread.join(3)
                assert not thread.is_alive() and outcomes == [0]


if __name__ == "__main__":
    with patch.object(socket, "socket", local_socket), patch.object(socket, "getaddrinfo", denied):
        result = run()
    print(json.dumps(result))  # noqa: T201 -- subprocess machine-readable result
