"""Strict codecs and hermetic AF_UNIX transport; the suite's network guard stays intact."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest

from tellyq.domain.queue import QueueEntry, QueueSnapshot
from tellyq.domain.values import (
    CommandAction,
    CommandOutcome,
    CommandReceipt,
    CompletionAttribution,
    CompletionEvidence,
    ContentKind,
    ContentRef,
    ErrorCode,
    PlaybackError,
    PlaybackObservation,
    PlaybackRequest,
    PlaybackScope,
    PlaybackTarget,
    PlayerState,
    SessionSnapshot,
)
from tellyq.models import IPCResponse
from tellyq.runner import RunnerCommand, RunnerSnapshot, SessionRunner, TaskView
from tellyq.runner_codec import (
    MAX_FRAME_BYTES,
    IPCProtocolError,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
    wire_command,
    wire_snapshot,
    wire_view,
)
from tellyq.runner_ipc import IPCUnavailable, RunnerIPCClient, RunnerIPCServer, read_frame

TARGET = PlaybackTarget("synthetic-device", "cast", "Synthetic display")


def frame(value):
    return (json.dumps(value) + "\n").encode()


def start_command(name="start"):
    return RunnerCommand(
        name,
        CommandAction.START,
        PlaybackRequest(name, name, "item", ContentRef("youtube", "synthetic"), TARGET),
    )


def submit(command):
    return encode_request(
        {"schema_version": 1, "operation": "submit", "command": wire_command(command)}
    )


class Task:
    def __init__(self):
        self.entered, self.release = Event(), Event()
        self.handled = []

    def step(self, cancellation):
        self.entered.set()
        assert self.release.wait(3)
        return TaskView()

    def handle(self, command, cancellation):
        self.handled.append(command)
        return TaskView()

    def close(self):
        pass


@pytest.fixture
def owner(tmp_path):
    task = Task()
    runner = SessionRunner(tmp_path / "runtime", TARGET, lambda _: task, step_interval=2)
    runner.start()
    assert task.entered.wait(1)
    yield runner, task
    task.release.set()
    runner.shutdown(2)


def test_import_and_construction_have_no_endpoint_or_thread_effects(tmp_path):
    code = """
import sys, threading
from pathlib import Path
def audit(event, args):
    if event.startswith('socket.') or event in {'os.mkdir', 'os.chmod'}:
        raise AssertionError(event)
sys.addaudithook(audit)
from tellyq.runner import SessionRunner
from tellyq.runner_ipc import RunnerIPCServer, RunnerIPCClient
from tellyq.domain.values import PlaybackTarget
before = threading.active_count()
runner = SessionRunner(Path('runtime'), PlaybackTarget('synthetic', 'cast'), lambda _: None)
RunnerIPCServer(Path('runtime'), runner)
RunnerIPCClient(Path('runtime'))
assert threading.active_count() == before
assert not Path('runtime').exists()
assert not any(name in sys.modules for name in ('pychromecast','zeroconf','chirp','milo'))
"""
    run_child(code, tmp_path)


@pytest.mark.parametrize("action", [CommandAction.STOP, CommandAction.PAUSE, CommandAction.RESUME])
def test_external_control_round_trip(action):
    command = RunnerCommand("id", action)
    assert decode_request(submit(command)).command == command


def test_start_round_trip_preserves_full_identity_and_metadata():
    command = start_command()
    decoded = decode_request(submit(command)).command
    assert decoded == command
    assert decoded is not None and decoded.request is not None
    assert decoded.request.target.name == TARGET.name


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": True, "operation": "status"},
        {"schema_version": 2, "operation": "status"},
        {"schema_version": 1, "operation": "status", "timeout": 0},
        {"schema_version": 1, "operation": "ticket", "command_id": " "},
        {"schema_version": 1, "operation": "shutdown", "timeout": True},
        {"schema_version": 1, "operation": "shutdown", "timeout": -1},
        {"schema_version": 1, "operation": "shutdown", "timeout": 6},
        {"schema_version": 1, "operation": "shutdown", "timeout": float("inf")},
        {"schema_version": 1, "operation": "submit", "command": {}},
        {"schema_version": 1, "operation": "__import__"},
        [],
    ],
)
def test_invalid_request_shapes_are_rejected(payload):
    with pytest.raises(IPCProtocolError):
        decode_request(frame(payload))


@pytest.mark.parametrize("action", ["release", "seek", "skip", "START", "", "future_command"])
def test_internal_or_future_actions_cannot_cross_ipc(action):
    payload = {
        "schema_version": 1,
        "operation": "submit",
        "command": {"command_id": "id", "action": action, "request": None},
    }
    with pytest.raises(IPCProtocolError):
        decode_request(frame(payload))


@pytest.mark.parametrize(
    "bad",
    [
        b'{"schema_version":1,"schema_version":1,"operation":"status"}\n',
        b"{}",
        b"{}\n{}\n",
        b"\xff\n",
        b"[" * 2000 + b"]" * 2000 + b"\n",
        b" " * MAX_FRAME_BYTES + b"\n",
    ],
)
def test_invalid_frames_are_rejected(bad):
    with pytest.raises(IPCProtocolError):
        decode_request(bad)


def test_start_request_is_validated_once_without_backend_import():
    value = json.loads(submit(start_command()))
    value["command"]["request"]["target"]["device_id"] = False
    with pytest.raises(IPCProtocolError):
        decode_request(frame(value))
    value = json.loads(submit(RunnerCommand("pause", CommandAction.PAUSE)))
    value["command"]["request"] = {}
    with pytest.raises(IPCProtocolError):
        decode_request(frame(value))


class Fragments:
    def __init__(self, parts):
        self.parts = iter(parts)
        self.timeouts = []

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def recv(self, _count):
        return next(self.parts, b"")


def test_fragment_reader_uses_one_total_deadline(monkeypatch):
    times = iter([0.0, 0.1, 0.8])
    monkeypatch.setattr("tellyq.runner_ipc.monotonic", lambda: next(times))
    connection = Fragments([b'{"schema_version":1,', b'"operation":"status"}\n'])
    assert decode_request(read_frame(connection, 1)).operation == "status"
    assert connection.timeouts == pytest.approx([0.9, 0.2])


def test_trickle_timeout_does_not_reset_deadline(monkeypatch):
    times = iter([0.0, 0.1, 1.1])
    monkeypatch.setattr("tellyq.runner_ipc.monotonic", lambda: next(times))
    with pytest.raises(IPCProtocolError, match="deadline"):
        read_frame(Fragments([b"{", b"}\n"]), 1)


@pytest.mark.parametrize("parts", [[b"{}"], [b"{}\n{}\n"], [b"x" * (MAX_FRAME_BYTES + 1)]])
def test_fragment_reader_rejects_incomplete_multiple_or_large_frames(parts):
    with pytest.raises(IPCProtocolError):
        read_frame(Fragments(parts), 1)


def test_typed_response_validation_rejects_invalid_nested_fields():
    good: IPCResponse = {
        "schema_version": 1,
        "ok": True,
        "code": "status",
        "snapshot": wire_snapshot(RunnerSnapshot()),
    }
    assert decode_response(encode_response(good)) == good
    for key, invalid in [
        ("phase", "teleported"),
        ("revision", True),
        ("pending_commands", -1),
        ("owns_device", 1),
        ("failure", "SECRET backend exception"),
        ("view", []),
    ]:
        bad = json.loads(encode_response(good))
        bad["snapshot"][key] = invalid
        with pytest.raises(IPCProtocolError):
            decode_response(frame(bad))
    bad = json.loads(encode_response(good))
    bad["ok"] = False
    with pytest.raises(IPCProtocolError):
        decode_response(frame(bad))


def test_projection_preserves_historical_evidence_and_omits_exception_text():
    when = datetime(2026, 9, 1, tzinfo=UTC)
    request = start_command().request
    assert request is not None
    scope = PlaybackScope(request, "session", "generation", 0)
    receipt = CommandReceipt(
        "request",
        "attempt",
        CommandAction.PAUSE,
        CommandOutcome.REJECTED,
        when,
        PlaybackError(ErrorCode.CONTROL_REJECTED, "SECRET message", "op", True, "SECRET recovery"),
        requested_at=when,
    )
    sample = SessionSnapshot(
        scope,
        state=PlayerState.ENDED,
        receipt=receipt,
        latest=PlaybackObservation(TARGET, "generation", 7, when, 12, position=13, ad_active=None),
        completion=CompletionEvidence(when, 10, 6, 5, CompletionAttribution.EXPLICIT, "receiver"),
    )
    queue = QueueSnapshot(
        "queue",
        TARGET,
        1,
        1,
        "item",
        False,
        (QueueEntry("item", ContentRef("youtube", "synthetic", ContentKind.EPISODE, "Title")),),
    )
    view = wire_view(TaskView(sample, queue))
    assert view["playback"] is not None and view["queue"] is not None
    playback = json.loads(json.dumps(view["playback"]))
    assert playback["observed_at"] == when.isoformat()
    assert playback["completion"]["observed_at"] == when.isoformat()
    assert playback["completion"]["historical"] is True
    assert playback["ad_active"] is None
    assert playback["receipt"]["recorded_at"] == when.isoformat()
    assert playback["receipt"]["error_code"] == "control_rejected"
    assert "SECRET" not in json.dumps(view)
    assert view["queue"]["target"] == {
        "device_id": TARGET.device_id,
        "route": "cast",
        "name": TARGET.name,
    }
    assert view["queue"]["items"] == [
        {
            "item_id": "item",
            "provider": "youtube",
            "content_id": "synthetic",
            "kind": "episode",
            "title": "Title",
            "intent": "pending",
        }
    ]


def test_queue_projection_bounds_items_but_keeps_first_and_current():
    queue = QueueSnapshot(
        "queue",
        TARGET,
        1,
        1,
        "200",
        False,
        tuple(QueueEntry(str(i), ContentRef("youtube", str(i))) for i in range(300)),
    )
    projected = json.loads(json.dumps(wire_view(TaskView(queue=queue))))["queue"]
    assert projected["item_count"] == 300 and projected["items_truncated"]
    assert len(projected["items"]) == 128
    assert projected["items"][0]["item_id"] == "0"
    assert projected["items"][-1]["item_id"] == "200"


def test_status_and_stop_are_responsive_while_task_is_blocked(owner):
    runner, task = owner
    server = RunnerIPCServer(Path("unused"), runner, ticket_capacity=1)
    first = server.handle_request(submit(start_command()))
    assert first["code"] == "accepted" and first["ticket"]["state"] == "queued"
    assert server.handle_request(submit(start_command("other")))["code"] == "history_full"
    status = server.handle_request(frame({"schema_version": 1, "operation": "status"}))
    assert status["snapshot"]["pending_commands"] == 1 and not task.release.is_set()
    stop = server.handle_request(submit(RunnerCommand("stop", CommandAction.STOP)))
    assert stop["code"] == "accepted"
    assert runner.snapshot().cancellation_requested and not task.release.is_set()
    result = server.handle_request(
        frame({"schema_version": 1, "operation": "ticket", "command_id": "start"})
    )
    assert result["ticket"]["state"] == "canceled"
    another = server.handle_request(submit(RunnerCommand("stop-again", CommandAction.STOP)))
    assert another["ticket_id"] == stop["ticket_id"] == "stop"
    assert (
        server.handle_request(submit(RunnerCommand("stop-again", CommandAction.PAUSE)))["code"]
        == "command_conflict"
    )
    assert (
        server.handle_request(submit(RunnerCommand("stop-again", CommandAction.STOP)))["code"]
        == "accepted"
    )
    assert (
        server.handle_request(submit(RunnerCommand("new-alias", CommandAction.STOP)))["code"]
        == "history_full"
    )
    assert (
        server.handle_request(
            frame({"schema_version": 1, "operation": "ticket", "command_id": "stop-again"})
        )["ticket_id"]
        == "stop"
    )
    assert (
        server.handle_request(submit(RunnerCommand("stop", CommandAction.PAUSE)))["code"]
        == "command_conflict"
    )
    assert (
        server.handle_request(submit(RunnerCommand("start", CommandAction.STOP)))["code"]
        == "command_conflict"
    )


def test_concurrent_duplicate_submission_shares_one_ticket_and_checks_all_payload_fields(owner):
    runner, _ = owner
    server = RunnerIPCServer(Path("unused"), runner)
    command = start_command()
    with ThreadPoolExecutor(max_workers=8) as workers:
        responses = list(workers.map(server.handle_request, [submit(command)] * 20))
    assert all(response["code"] == "accepted" for response in responses)
    assert runner.snapshot().pending_commands == 1
    assert command.request is not None
    changed = replace(
        command, request=replace(command.request, target=replace(TARGET, name="Other"))
    )
    assert server.handle_request(submit(changed))["code"] == "command_conflict"
    assert (
        server.handle_request(submit(RunnerCommand("start", CommandAction.PAUSE)))["code"]
        == "command_conflict"
    )


def test_completed_commands_remain_deduplicated_and_missing_is_explicit(owner):
    runner, task = owner
    server = RunnerIPCServer(Path("unused"), runner)
    command = start_command()
    server.handle_request(submit(command))
    task.release.set()
    runner.submit(command).wait(1)
    assert server.handle_request(submit(command))["ticket"]["state"] == "handled"
    assert len(task.handled) == 1
    missing = server.handle_request(
        frame({"schema_version": 1, "operation": "ticket", "command_id": "unknown"})
    )
    assert missing == {"schema_version": 1, "ok": False, "code": "ticket_missing"}


def test_shutdown_timeout_retains_endpoint_ownership_and_safe_status(owner):
    runner, task = owner
    server = RunnerIPCServer(Path("unused"), runner)
    result = server.handle_request(
        frame({"schema_version": 1, "operation": "shutdown", "timeout": 0})
    )
    assert result["code"] == "shutdown_timeout" and not result["ok"]
    assert result["snapshot"]["owns_device"] and result["snapshot"]["cancellation_requested"]
    with pytest.raises(IPCUnavailable, match="still owns"):
        server.close()
    assert not task.release.is_set()


def test_boundary_errors_never_publish_arbitrary_exception_text(owner):
    runner, _ = owner
    server = RunnerIPCServer(Path("unused"), runner)
    assert server.handle_request(b"bad\n")["code"] == "invalid_request"
    fake = Mock(spec=SessionRunner)
    fake.snapshot.side_effect = RuntimeError("SECRET hostname and token")
    result = RunnerIPCServer(Path("unused"), fake).handle_request(
        frame({"schema_version": 1, "operation": "status"})
    )
    assert result == {"schema_version": 1, "ok": False, "code": "internal_error"}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_clients": 1},
        {"max_clients": True},
        {"ticket_capacity": 0},
        {"request_timeout": float("nan")},
    ],
)
def test_server_limits_are_validated(kwargs, tmp_path):
    runner = SessionRunner(tmp_path, TARGET, lambda _: Task())
    with pytest.raises(ValueError):
        RunnerIPCServer(tmp_path, runner, **kwargs)


def test_client_does_not_fall_back_when_endpoint_missing_or_not_private(tmp_path):
    client = RunnerIPCClient(tmp_path)
    with pytest.raises(IPCUnavailable, match="Do not fall back"):
        client.status()
    (tmp_path / "runner.sock").write_text("do not remove")
    with pytest.raises(IPCUnavailable, match="private"):
        client.status()
    assert (tmp_path / "runner.sock").read_text() == "do not remove"


def run_child(code, tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        capture_output=True,
        text=True,
        timeout=8,
    )
    assert result.returncode == 0, result.stderr


# The subprocess is the only socket-capable test surface. Guard it before importing
# implementation code, and prove IPv4/IPv6 and DNS are rejected there as well.
UNIX_GUARD = r"""
import os, socket, sys, tempfile, threading, stat, json
from pathlib import Path
from threading import Event
from time import monotonic

def audit(event, args):
    if event == 'socket.__new__' and args[1] != socket.AF_UNIX:
        raise AssertionError('Only hermetic AF_UNIX is allowed')
    if event in {'socket.getaddrinfo', 'socket.gethostbyname', 'socket.gethostbyname_ex', 'socket.gethostbyaddr'}:
        raise AssertionError('DNS is forbidden')
sys.addaudithook(audit)
for family in (socket.AF_INET, socket.AF_INET6):
    try: socket.socket(family, socket.SOCK_STREAM)
    except AssertionError: pass
    else: raise AssertionError('network guard failed')
try: socket.getaddrinfo('forbidden.invalid', 80)
except AssertionError: pass
else: raise AssertionError('DNS guard failed')
from tellyq.runner import SessionRunner, RunnerCommand, TaskView
from tellyq.runner_ipc import RunnerIPCServer, RunnerIPCClient, IPCUnavailable, endpoint_path, read_frame
from tellyq.runner_codec import decode_response, encode_request
from tellyq.domain.values import CommandAction, PlaybackTarget, ContentRef, PlaybackRequest
TARGET = PlaybackTarget('synthetic', 'cast')
class Task:
    def __init__(self):
        self.entered, self.release, self.handled = Event(), Event(), Event()
        self.effects = []
    def step(self, cancellation):
        self.entered.set()
        assert self.release.wait(5)
        return TaskView()
    def handle(self, command, cancellation):
        self.effects.append(command.action)
        self.handled.set()
        return TaskView()
    def close(self): pass

def new_owner(runtime):
    task = Task()
    runner = SessionRunner(runtime, TARGET, lambda _: task, step_interval=2)
    runner.start()
    assert task.entered.wait(1)
    return runner, task
"""


def test_unix_transport_status_stop_fragmentation_and_blocked_shutdown(tmp_path):
    run_child(
        UNIX_GUARD
        + r"""
with tempfile.TemporaryDirectory(prefix='tq-', dir='/tmp') as directory:
    runtime = Path(directory)
    runner, task = new_owner(runtime)
    server = RunnerIPCServer(runtime, runner)
    client = RunnerIPCClient(runtime)
    try:
        server.start()
        assert stat.S_IMODE(runtime.stat().st_mode) == 0o700
        assert stat.S_IMODE(endpoint_path(runtime).stat().st_mode) == 0o600
        # A slow client holds one handler without stopping independent cached reads.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as slow:
            slow.connect(str(endpoint_path(runtime)))
            slow.sendall(b'{')
            status = client.status()
            assert status['code'] == 'status' and status['snapshot']['owns_device']
            request = PlaybackRequest('request','attempt','item',ContentRef('youtube','synthetic'),TARGET)
            submitted = client.submit(RunnerCommand('start',CommandAction.START,request))
            assert submitted['ticket']['state'] == 'queued'
            stopped = client.submit(RunnerCommand('stop',CommandAction.STOP))
            assert stopped['code'] == 'accepted'
            assert client.ticket('start')['ticket']['state'] == 'canceled'
            assert not task.release.is_set()
            timeout = client.shutdown(0)
            assert timeout['code'] == 'shutdown_timeout' and timeout['snapshot']['owns_device']
            try: server.close()
            except IPCUnavailable: pass
            else: raise AssertionError('closed live owner endpoint')
            assert endpoint_path(runtime).exists()
            assert client.status()['snapshot']['owns_device']
        task.release.set()
        runner.shutdown(1)
        assert task.effects == [CommandAction.STOP]
        assert client.status()['snapshot']['phase'] == 'stopped'
        assert client.shutdown(0)['code'] == 'shutdown'
        server.close()
        assert not endpoint_path(runtime).exists()
    finally:
        task.release.set()
        runner.shutdown(2)
        server.close()
""",
        tmp_path,
    )


def test_unix_endpoint_refuses_clobber_and_competing_servers(tmp_path):
    run_child(
        UNIX_GUARD
        + r"""
with tempfile.TemporaryDirectory(prefix='tq-', dir='/tmp') as directory:
    runtime = Path(directory)
    runner, task = new_owner(runtime)
    path = endpoint_path(runtime)
    servers = []
    try:
        # A file and a symlink are never removed or replaced.
        path.write_text('sentinel')
        for symlink in (False, True):
            if symlink:
                path.unlink()
                path.symlink_to(runtime / 'missing')
            candidate = RunnerIPCServer(runtime, runner)
            try: candidate.start()
            except IPCUnavailable: pass
            else: raise AssertionError('unsafe endpoint overwritten')
            assert path.is_symlink() if symlink else path.read_text() == 'sentinel'
        path.unlink()
        # A live endpoint outside our lease is also not unlinked.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as external:
            external.bind(str(path)); external.listen(1)
            identity = path.stat().st_ino
            try: RunnerIPCServer(runtime, runner).start()
            except IPCUnavailable: pass
            else: raise AssertionError('live endpoint stolen')
            assert path.stat().st_ino == identity
        # That now-stale socket is safely reclaimed after its listener closes.
        server = RunnerIPCServer(runtime, runner); servers.append(server)
        server.start()
        identity = path.stat().st_ino
        try: RunnerIPCServer(runtime, runner).start()
        except IPCUnavailable: pass
        else: raise AssertionError('competing endpoint owner')
        assert path.stat().st_ino == identity
        assert RunnerIPCClient(runtime).status()['code'] == 'status'
        task.release.set(); runner.shutdown(1)
        # Cleanup will not remove a replacement path with a different inode/type.
        path.unlink(); path.write_text('replacement')
        server.close()
        assert path.read_text() == 'replacement'
    finally:
        task.release.set(); runner.shutdown(2)
        for server in servers: server.close()
""",
        tmp_path,
    )


def test_unix_handler_saturation_is_bounded_and_does_not_queue_threads(tmp_path):
    run_child(
        UNIX_GUARD
        + r"""
with tempfile.TemporaryDirectory(prefix='tq-', dir='/tmp') as directory:
    runtime = Path(directory)
    runner, task = new_owner(runtime)
    server = RunnerIPCServer(runtime, runner, max_clients=2, request_timeout=1)
    sockets = []
    entered, release = Event(), Event()
    original = server.handle_request
    def blocked(frame):
        entered.set()
        assert release.wait(3)
        return original(frame)
    server.handle_request = blocked
    try:
        server.start()
        request = encode_request({'schema_version':1,'operation':'status'})
        for _ in range(2):
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.connect(str(endpoint_path(runtime))); connection.sendall(request)
            sockets.append(connection)
        assert entered.wait(1)
        # Use the private lock only as a test barrier, never timing sleeps.
        deadline = monotonic() + 1
        while True:
            with server._clients_lock:
                count = len(server._clients)
            if count == 2: break
            assert monotonic() < deadline
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as overflow:
            overflow.connect(str(endpoint_path(runtime)))
            assert decode_response(read_frame(overflow,1))['code'] == 'server_busy'
        assert len([t for t in threading.enumerate() if t.name == 'tellyq-ipc-client']) == 2
        release.set()
        for connection in sockets:
            assert decode_response(read_frame(connection,1))['code'] == 'status'
    finally:
        release.set()
        for connection in sockets: connection.close()
        task.release.set(); runner.shutdown(2); server.close()
""",
        tmp_path,
    )


def test_mailbox_full_and_mismatched_target_do_not_add_ticket_history(owner):
    runner, _ = owner
    server = RunnerIPCServer(Path("unused"), runner)
    for number in range(16):
        assert server.handle_request(submit(start_command(str(number))))["code"] == "accepted"
    assert server.handle_request(submit(start_command("overflow")))["code"] == "mailbox_full"
    assert (
        server.handle_request(
            frame({"schema_version": 1, "operation": "ticket", "command_id": "overflow"})
        )["code"]
        == "ticket_missing"
    )
    assert (
        server.handle_request(submit(RunnerCommand("stop", CommandAction.STOP)))["code"]
        == "accepted"
    )
    assert (
        server.handle_request(submit(start_command("after-stop")))["code"] == "runner_unavailable"
    )


def test_mismatched_target_is_rejected_before_task_effect(owner):
    runner, task = owner
    server = RunnerIPCServer(Path("unused"), runner)
    command = start_command()
    assert command.request is not None
    changed = replace(
        command, request=replace(command.request, target=PlaybackTarget("other", "cast"))
    )
    assert server.handle_request(submit(changed))["code"] == "command_rejected"
    assert not task.handled and runner.snapshot().pending_commands == 0


def test_host_stop_coalesces_with_safe_canonical_ticket_identity(owner):
    runner, _ = owner
    runner.stop("host-stop")
    server = RunnerIPCServer(Path("unused"), runner)
    response = server.handle_request(submit(RunnerCommand("client-stop", CommandAction.STOP)))
    assert response["ticket_id"] == "host-stop"
    assert (
        server.handle_request(submit(RunnerCommand("client-stop", CommandAction.PAUSE)))["code"]
        == "command_conflict"
    )
    assert (
        server.handle_request(submit(RunnerCommand("host-stop", CommandAction.PAUSE)))["code"]
        == "command_conflict"
    )


def test_unix_malformed_requests_unknown_outcomes_and_cross_process_client(tmp_path):
    run_child(
        UNIX_GUARD
        + r'''
import subprocess
with tempfile.TemporaryDirectory(prefix='tq-', dir='/tmp') as directory:
    runtime = Path(directory)
    runner, task = new_owner(runtime)
    server = RunnerIPCServer(runtime, runner)
    try:
        server.start()
        for bad in (b'not json\n', b'{}\n{}\n', b'x' * 65537):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.connect(str(endpoint_path(runtime)))
                connection.sendall(bad)
                assert decode_response(read_frame(connection,1))['code'] == 'invalid_request'
        code = """
import socket,sys,json
from pathlib import Path
def audit(event,args):
    if event == 'socket.__new__' and args[1] != socket.AF_UNIX: raise AssertionError('network')
    if event.startswith('socket.get'): raise AssertionError('DNS')
sys.addaudithook(audit)
from tellyq.runner_ipc import RunnerIPCClient
response = RunnerIPCClient(Path(sys.argv[1])).status()
assert response['code'] == 'status' and response['snapshot']['owns_device']
"""
        result = subprocess.run([sys.executable,'-c',code,str(runtime)], capture_output=True,text=True,timeout=2)
        assert result.returncode == 0, result.stderr
        # A response can be lost after acceptance. Retry with the same identity,
        # then query; never manufacture remote acknowledgement from local status.
        command = RunnerCommand('pause',CommandAction.PAUSE)
        from tellyq.runner_codec import wire_command
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as abandoned:
            abandoned.connect(str(endpoint_path(runtime)))
            abandoned.sendall(encode_request({'schema_version':1,'operation':'submit','command':wire_command(command)}))
        repeated = RunnerIPCClient(runtime).submit(command)
        assert repeated['code'] == 'accepted'
        assert repeated['ticket']['state'] == 'queued'
        assert runner.snapshot().pending_commands == 1
    finally:
        task.release.set(); runner.shutdown(2); server.close()
''',
        tmp_path,
    )


def test_unix_close_delivers_shutdown_response_before_cleanup(tmp_path):
    run_child(
        UNIX_GUARD
        + r"""
with tempfile.TemporaryDirectory(prefix='tq-', dir='/tmp') as directory:
    runtime = Path(directory)
    runner, task = new_owner(runtime)
    server = RunnerIPCServer(runtime, runner)
    response_ready, release_response, close_joining = Event(), Event(), Event()
    original_handle, original_join = server.handle_request, threading.Thread.join
    replies, errors = [], []
    def delayed_response(frame):
        response = original_handle(frame)
        response_ready.set()
        assert release_response.wait(3)
        return response
    def observed_join(thread, timeout=None):
        if threading.current_thread().name == 'closer' and thread.name == 'tellyq-ipc-client':
            close_joining.set()
        return original_join(thread, timeout)
    def client_shutdown():
        try: replies.append(RunnerIPCClient(runtime).shutdown(1))
        except Exception as exc: errors.append(type(exc).__name__)
    def close_endpoint():
        try: server.close(2)
        except Exception as exc: errors.append(type(exc).__name__)
    server.handle_request = delayed_response
    threading.Thread.join = observed_join
    client_thread = threading.Thread(target=client_shutdown)
    close_thread = threading.Thread(target=close_endpoint, name='closer')
    try:
        server.start()
        task.release.set()
        client_thread.start()
        assert response_ready.wait(1)
        assert not runner.snapshot().owns_device
        close_thread.start()
        assert close_joining.wait(1)
        # Close has reached its graceful join while the final reply is withheld.
        release_response.set()
        client_thread.join(2); close_thread.join(2)
        assert not errors, errors
        assert replies[0]['code'] == 'shutdown' and replies[0]['ok']
        assert not endpoint_path(runtime).exists()
    finally:
        release_response.set(); task.release.set()
        runner.shutdown(2)
        if client_thread.ident: client_thread.join(2)
        if close_thread.ident: close_thread.join(2)
        server.close()
        threading.Thread.join = original_join
""",
        tmp_path,
    )
