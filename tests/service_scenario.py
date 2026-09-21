"""Subprocess-only AF_UNIX composition scenario; all internet sockets/DNS are denied."""

import io
import json
import socket
import tempfile
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from threading import Thread, get_ident
from time import monotonic, sleep
from unittest.mock import patch

from tellyq import __main__ as cli
from tellyq import service
from tellyq.domain.queue import QueueEntry
from tellyq.domain.values import (
    CommandAction,
    CommandOutcome,
    CommandReceipt,
    ContentRef,
    IdentityUpdate,
    IdleReason,
    PlaybackCapabilities,
    PlaybackObservation,
    PlaybackTarget,
    PlayerState,
)
from tellyq.runner_ipc import RunnerIPCClient, endpoint_path
from tests.playback_support import FakeClock

_socket = socket.socket


def local_socket(family=socket.AF_INET, *args, **kwargs):
    if family != socket.AF_UNIX:
        raise AssertionError("Only the local Unix endpoint is permitted.")
    return _socket(family, *args, **kwargs)


def denied(*args, **kwargs):
    raise AssertionError("DNS is forbidden.")


class Backend:
    def __init__(self, clock):
        self.clock = clock
        self.request = None
        self.active = False
        self.sequence = 0
        self.reads = 0
        self.effects = []
        self.threads = set()

    def capabilities(self, target):
        return PlaybackCapabilities()

    def receipt(self, request, action):
        return CommandReceipt(
            request.request_id,
            request.attempt_id,
            action,
            CommandOutcome.ACCEPTED,
            self.clock.utcnow(),
        )

    def start(self, request):
        self.threads.add(get_ident())
        self.effects.append("start")
        self.request, self.active, self.reads = request, True, 0
        return self.receipt(request, CommandAction.START)

    def stop(self, scope):
        self.threads.add(get_ident())
        self.effects.append("stop")
        self.active = False
        return self.receipt(scope.request, CommandAction.STOP)

    def event(self, target, **kwargs):
        self.sequence += 1
        self.clock.tick(0.1)
        return PlaybackObservation(
            target,
            "synthetic-connection",
            self.sequence,
            self.clock.utcnow(),
            self.clock.monotonic(),
            **kwargs,
        )

    def observe(self, target):
        self.threads.add(get_ident())
        if not self.active:
            return (self.event(target, session_active=False),)
        assert self.request is not None
        self.reads += 1
        identity = {"session_id": self.request.attempt_id, "application_id": "synthetic-app"}
        receiver = self.event(target, session_active=True, **identity)
        if self.reads == 1:
            events = [receiver]
            for position in (1.0, 2.2):
                self.clock.tick(1.1)
                events.append(
                    self.event(
                        target,
                        content=self.request.content,
                        identity_update=IdentityUpdate.EXPLICIT,
                        playback_id="media",
                        state=PlayerState.PLAYING,
                        position=position,
                        duration=3.0,
                        ad_active=False,
                        **identity,
                    )
                )
            return tuple(events)
        return (
            receiver,
            self.event(
                target,
                content=self.request.content,
                identity_update=IdentityUpdate.EXPLICIT,
                playback_id="media",
                state=PlayerState.IDLE,
                idle_reason=IdleReason.FINISHED,
                position=3.0,
                duration=3.0,
                ad_active=False,
                **identity,
            ),
        )


def wait_until(test, timeout=5):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if test():
            return
        sleep(0.01)
    raise AssertionError("Composition condition did not become true.")


def run():
    patch.object(socket, "socket", local_socket).start()
    patch.object(socket, "getaddrinfo", denied).start()
    with tempfile.TemporaryDirectory(prefix="tq-service-", dir="/tmp") as directory:
        runtime = Path(directory) / "runtime"
        target = PlaybackTarget("00000000-0000-4000-8000-000000000001", "cast")
        entries = tuple(
            QueueEntry(str(index), ContentRef("youtube", content))
            for index, content in enumerate(("f9F7yDjSdNA", "hgsIFyITvJE", "f9F7yDjSdNA"))
        )
        spec = service.QueueSpec("synthetic-session", target, entries)
        backend = Backend(FakeClock())
        events, outcomes = [], []

        @contextmanager
        def factory(_target, _clock):
            backend.threads.add(get_ident())
            try:
                yield backend
            finally:
                backend.threads.add(get_ident())

        def serve():
            outcomes.append(
                service.run_foreground(runtime, spec, events.append, backend_factory=factory)
            )

        with patch.object(service, "SystemClock", return_value=backend.clock):
            thread = Thread(target=serve)
            thread.start()
            client = RunnerIPCClient(runtime)
            try:
                wait_until(lambda: endpoint_path(runtime).exists())
                wait_until(lambda: client.status()["snapshot"]["view"]["queue"] is not None)
                assert backend.effects == []
                output = io.StringIO()
                with redirect_stdout(output):
                    assert cli.main(["--runtime", str(runtime), "start"]) == 0
                accepted = json.loads(output.getvalue())
                assert accepted["code"] == "accepted"
                command_id = accepted["ticket_id"]
                wait_until(lambda: backend.effects.count("stop") == 3)

                def released():
                    playback = client.status()["snapshot"]["view"]["playback"]
                    return playback is not None and playback["release_confirmed"] is True

                wait_until(released)
                current = client.status()["snapshot"]["view"]["queue"]
                assert current is not None
                items = current["items"]
                assert isinstance(items, list)
                assert all(
                    isinstance(item, dict) and item["intent"] == "finished" for item in items
                )
                # Stable CLI retry must not start item 1 or item 3 a second time.
                with redirect_stdout(io.StringIO()):
                    assert cli.main(["--runtime", str(runtime), "start"]) == 0
                assert client.ticket(command_id)["ticket"]["state"] == "handled"
                assert backend.effects == ["start", "stop"] * 3
            finally:
                if thread.is_alive() and endpoint_path(runtime).exists():
                    client.shutdown()
                thread.join(2)
            assert not thread.is_alive() and outcomes == [0]
            assert not endpoint_path(runtime).exists()
            assert len(backend.threads) == 1 and get_ident() not in backend.threads
            assert events[0]["event"] == "ready" and events[-1]["event"] == "closed"
            # Reopening durable finished history is not permission to replay it.
            restarted = Thread(target=serve)
            restarted.start()
            try:
                wait_until(lambda: endpoint_path(runtime).exists())
                recovered = client.status()["snapshot"]["view"]["queue"]
                assert recovered is not None and recovered["cancellation_requested"] is True
                with redirect_stdout(io.StringIO()):
                    assert cli.main(["--runtime", str(runtime), "start"]) == 0
                wait_until(lambda: client.ticket(command_id)["ticket"]["state"] == "handled")
                assert backend.effects == ["start", "stop"] * 3
            finally:
                if restarted.is_alive() and endpoint_path(runtime).exists():
                    client.shutdown()
                restarted.join(2)
            assert not restarted.is_alive() and outcomes == [0, 0]
        return {
            "starts": 3,
            "releases": 3,
            "finished": 3,
            "single_owner": True,
            "recovery_held": True,
        }


if __name__ == "__main__":
    import sys

    sys.stdout.write(json.dumps(run()))
