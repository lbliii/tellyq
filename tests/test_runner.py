"""Owner concurrency checked with barriers/events and private files; no device I/O."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
from threading import Barrier, Event, current_thread, get_ident
from unittest.mock import Mock

import pytest

from tellyq.domain.values import CommandAction, ContentRef, PlaybackRequest, PlaybackTarget
from tellyq.runner import (
    MailboxFull,
    RunnerCommand,
    RunnerFailure,
    RunnerPhase,
    RunnerTimeout,
    RunnerUnavailable,
    SessionRunner,
    TaskView,
    TicketState,
)
from tellyq.state import command_lock

TARGET = PlaybackTarget("synthetic-device", "cast")


def start_command(name):
    return RunnerCommand(
        name,
        CommandAction.START,
        PlaybackRequest(name, name, "item", ContentRef("youtube", "synthetic"), TARGET),
    )


class Task:
    def __init__(self, cancellation):
        self.cancellation = cancellation
        self.calls = [("factory", get_ident())]
        self.step_entered = Event()
        self.step_release = Event()
        self.handle_entered = Event()
        self.handle_release = Event()
        self.close_entered = Event()
        self.close_release = Event()
        self.step_release.set()
        self.handle_release.set()
        self.close_release.set()
        self.handled = []
        self.effects = []
        self.closed = False
        self.failure = None
        self.daemon = current_thread().daemon

    def step(self, cancellation):
        assert cancellation is self.cancellation
        self.calls.append(("step", get_ident()))
        self.step_entered.set()
        assert self.step_release.wait(3)
        if self.failure == "step":
            raise OSError("SECRET address token")
        return TaskView()

    def handle(self, command, cancellation):
        assert cancellation is self.cancellation
        self.calls.append(("handle", get_ident()))
        self.handled.append(command.action)
        self.handle_entered.set()
        assert self.handle_release.wait(3)
        if self.failure == "handle":
            raise OSError("SECRET address token")
        # Final effect-boundary check; a selected command can be canceled while
        # its preparation is blocked. Stop remains explicitly permitted.
        if (
            command.action not in {CommandAction.START, CommandAction.RESUME}
            or not cancellation.requested
        ):
            self.effects.append(command.action)
        return TaskView()

    def close(self):
        self.calls.append(("close", get_ident()))
        self.close_entered.set()
        assert self.close_release.wait(3)
        self.closed = True
        if self.failure == "close":
            raise OSError("SECRET address token")


@pytest.fixture
def owner(tmp_path):
    tasks = []

    def factory(cancellation):
        task = Task(cancellation)
        tasks.append(task)
        return task

    runner = SessionRunner(tmp_path / "runtime", TARGET, factory, step_interval=2)
    yield runner, tasks
    if tasks:
        tasks[0].step_release.set()
        tasks[0].handle_release.set()
        tasks[0].close_release.set()
    runner.shutdown(3)


def test_import_and_construction_are_inert(tmp_path):
    code = """
import sys
from pathlib import Path
def audit(event, args):
    if event.startswith('socket.') or event in {'os.mkdir','os.chmod'}:
        raise AssertionError(event)
sys.addaudithook(audit)
from tellyq.runner import SessionRunner
from tellyq.domain.values import PlaybackTarget
SessionRunner(Path('runtime'),PlaybackTarget('synthetic','cast'),lambda cancellation: None)
assert not Path('runtime').exists()
assert not any(name in sys.modules for name in ('pychromecast','zeroconf','milo','chirp'))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr


def test_factory_commands_step_and_close_use_one_non_daemon_owner(owner):
    runner, tasks = owner
    assert runner.snapshot().phase == RunnerPhase.NEW
    runner.start()
    task = tasks[0]
    assert task.step_entered.wait(1)
    result = runner.submit(start_command("first")).wait(1)
    assert result.state == TicketState.HANDLED
    assert task.effects == [CommandAction.START]
    final = runner.shutdown(1)
    assert final.phase == RunnerPhase.STOPPED and not final.owns_device
    assert task.closed and not task.daemon
    threads = {ident for _, ident in task.calls}
    assert len(threads) == 1 and get_ident() not in threads
    assert {"factory", "step", "handle", "close"} == {name for name, _ in task.calls}
    with pytest.raises(RunnerUnavailable):
        runner.start()
    with pytest.raises(RunnerUnavailable):
        runner.submit(start_command("later"))


def test_bounded_mailbox_and_reserved_priority_stop_during_blocked_step(tmp_path):
    made = []

    def factory(cancellation):
        task = Task(cancellation)
        task.step_release.clear()
        made.append(task)
        return task

    runner = SessionRunner(
        tmp_path / "runtime", TARGET, factory, mailbox_capacity=2, step_interval=2
    )
    try:
        runner.start()
        task = made[0]
        assert task.step_entered.wait(1)
        first = runner.submit(start_command("first"))
        pause = runner.submit(RunnerCommand("pause", CommandAction.PAUSE))
        with pytest.raises(MailboxFull):
            runner.submit(start_command("overflow"))
        stop = runner.stop("stop")
        assert runner.stop("another-stop") is stop
        assert first.wait(0).state == TicketState.CANCELED
        assert runner.snapshot().pending_commands == 2
        assert runner.snapshot().cancellation_requested
        assert task.cancellation.requested and task.effects == []
        with pytest.raises(RunnerUnavailable):
            runner.submit(start_command("after-stop"))
        with pytest.raises(RunnerUnavailable):
            runner.submit(RunnerCommand("resume", CommandAction.RESUME))
        task.step_release.set()
        assert stop.wait(1).state == TicketState.HANDLED
        assert pause.wait(1).state == TicketState.HANDLED
        assert task.effects == [CommandAction.STOP, CommandAction.PAUSE]
    finally:
        made[0].step_release.set()
        runner.shutdown(3)


def test_stop_latch_reaches_inflight_effect_boundary_and_status_is_responsive(owner):
    runner, tasks = owner
    runner.start()
    task = tasks[0]
    assert task.step_entered.wait(1)
    task.handle_release.clear()
    start = runner.submit(start_command("first"))
    try:
        assert task.handle_entered.wait(1)
        with ThreadPoolExecutor(max_workers=2) as executor:
            status = executor.submit(runner.snapshot).result(timeout=1)
            stop = executor.submit(runner.stop, "stop").result(timeout=1)
        assert status.active_command == start.snapshot().command
        assert task.cancellation.wait(0)
        assert stop.snapshot().state == TicketState.QUEUED
        assert task.effects == []
        task.handle_release.set()
        assert start.wait(1).state == TicketState.HANDLED
        assert stop.wait(1).state == TicketState.HANDLED
        assert task.effects == [CommandAction.STOP]
    finally:
        task.handle_release.set()


def test_local_cancel_removes_pending_starts_without_sending_stop(tmp_path):
    made = []

    def factory(cancellation):
        task = Task(cancellation)
        task.step_release.clear()
        made.append(task)
        return task

    runner = SessionRunner(tmp_path / "runtime", TARGET, factory)
    try:
        runner.start()
        assert made[0].step_entered.wait(1)
        queued = runner.submit(start_command("first"))
        assert runner.cancel().cancellation_requested
        assert queued.wait(0).state == TicketState.CANCELED
        made[0].step_release.set()
    finally:
        made[0].step_release.set()
        runner.shutdown(3)
    assert made[0].effects == []


def test_concurrent_producers_respect_capacity_and_coalesce_pending_identity(tmp_path):
    made = []

    def factory(cancellation):
        task = Task(cancellation)
        task.step_release.clear()
        made.append(task)
        return task

    runner = SessionRunner(tmp_path / "runtime", TARGET, factory, mailbox_capacity=3)
    try:
        runner.start()
        assert made[0].step_entered.wait(1)
        barrier = Barrier(8)

        def submit(number):
            barrier.wait(timeout=2)
            try:
                return runner.submit(start_command(str(number)))
            except MailboxFull:
                return None

        with ThreadPoolExecutor(max_workers=8) as executor:
            tickets = list(executor.map(submit, range(8)))
        accepted = [ticket for ticket in tickets if ticket is not None]
        assert len(accepted) == runner.snapshot().pending_commands == 3
        ticket = accepted[0]
        assert runner.submit(ticket.snapshot().command) is ticket
        with pytest.raises(ValueError, match="different work"):
            runner.submit(RunnerCommand(ticket.snapshot().command.command_id, CommandAction.PAUSE))
        stop_barrier = Barrier(8)

        def stop(number):
            stop_barrier.wait(timeout=2)
            return runner.stop(f"stop-{number}")

        with ThreadPoolExecutor(max_workers=8) as executor:
            stops = list(executor.map(stop, range(8)))
        assert all(ticket is stops[0] for ticket in stops)
        assert runner.snapshot().pending_commands == 1
        assert all(ticket.wait(0).state == TicketState.CANCELED for ticket in accepted)
    finally:
        made[0].step_release.set()
        runner.shutdown(3)


def test_shutdown_timeout_keeps_locks_and_closes_only_on_owner_thread(owner, tmp_path):
    runner, tasks = owner
    runner.start()
    task = tasks[0]
    assert task.step_entered.wait(1)
    task.close_release.clear()
    try:
        with pytest.raises(RunnerTimeout):
            runner.shutdown(0)
        assert task.close_entered.wait(1)
        assert runner.snapshot().phase == RunnerPhase.STOPPING
        assert runner.snapshot().owns_device
        with pytest.raises(RuntimeError), command_lock(tmp_path / "runtime"):
            pytest.fail("legacy command entered while task was closing")
        with pytest.raises(RunnerTimeout):
            runner.shutdown(0)
        assert not task.closed
    finally:
        task.close_release.set()
    assert runner.shutdown(1).phase == RunnerPhase.STOPPED
    with command_lock(tmp_path / "runtime"):
        pass


def test_initialization_timeout_retains_ownership_and_cancellation_reaches_factory(tmp_path):
    entered, release = Event(), Event()
    made = []

    def factory(cancellation):
        entered.set()
        assert release.wait(3)
        assert cancellation.requested
        task = Task(cancellation)
        made.append(task)
        return task

    runner = SessionRunner(tmp_path / "runtime", TARGET, factory)
    try:
        with pytest.raises(RunnerTimeout):
            runner.start(0)
        assert entered.wait(1)
        assert runner.snapshot().owns_device
        with pytest.raises(RunnerTimeout):
            runner.shutdown(0)
        with pytest.raises(RuntimeError), command_lock(tmp_path / "runtime"):
            pytest.fail("ownership escaped during initialization")
    finally:
        release.set()
    assert runner.shutdown(1).phase == RunnerPhase.STOPPED
    assert made[0].calls == [("factory", made[0].calls[0][1]), ("close", made[0].calls[0][1])]


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("handle", RunnerFailure.COMMAND),
        ("step", RunnerFailure.STEP),
        ("close", RunnerFailure.CLOSE),
    ],
)
def test_task_failure_is_safe_closes_once_and_releases_ownership(tmp_path, failure, expected):
    made = []

    def factory(cancellation):
        task = Task(cancellation)
        task.failure = failure
        # Hold first step so start can return deterministically before a failure.
        task.step_release.clear()
        made.append(task)
        return task

    runner = SessionRunner(tmp_path / "runtime", TARGET, factory, step_interval=2)
    runner.start()
    task = made[0]
    assert task.step_entered.wait(1)
    ticket = runner.submit(start_command("start")) if failure == "handle" else None
    task.step_release.set()
    if ticket:
        assert ticket.wait(1).state == TicketState.FAILED
    if failure == "step":
        assert task.close_entered.wait(1)
    final = runner.shutdown(1)
    assert final.phase == RunnerPhase.FAILED and final.failure == expected
    assert "SECRET" not in repr(final)
    assert [name for name, _ in task.calls].count("close") == 1
    with command_lock(tmp_path / "runtime"):
        pass


def test_factory_failure_never_leaks_arbitrary_error_or_lock(tmp_path):
    factory = Mock(side_effect=ValueError("SECRET address token"))
    runner = SessionRunner(tmp_path / "runtime", TARGET, factory)
    with pytest.raises(RunnerUnavailable, match="cached failure"):
        runner.start()
    final = runner.shutdown(1)
    assert final.failure == RunnerFailure.INITIALIZATION
    assert "SECRET" not in repr(final)
    with command_lock(tmp_path / "runtime"):
        pass


def test_competing_owner_and_legacy_commands_excluded_across_processes(owner, tmp_path):
    runner, _ = owner
    runner.start()
    script = """
import fcntl,json,sys
from pathlib import Path
from hashlib import sha256
from tellyq.state import command_lock
from tellyq.runner import SessionRunner,RunnerUnavailable,TaskView
from tellyq.domain.values import PlaybackTarget
runtime=Path(sys.argv[1])
results=[]
try:
    with command_lock(runtime):
        results.append('legacy-entered')
except RuntimeError:
    results.append('legacy-blocked')
class Task:
    def step(self,cancellation): return TaskView()
    def handle(self,command,cancellation): return TaskView()
    def close(self): pass
runner=SessionRunner(runtime,PlaybackTarget('synthetic-device','cast'),lambda cancellation: Task())
try:
    runner.start()
    results.append('runner-entered')
except RunnerUnavailable:
    results.append('runner-blocked')
finally:
    runner.shutdown(2)
with (runtime/'owners'/(sha256(b'synthetic-device').hexdigest()+'.lock')).open('a') as file:
    try:
        fcntl.flock(file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        results.append('receiver-entered')
    except BlockingIOError:
        results.append('receiver-blocked')
print(json.dumps(results))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "runtime")],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["legacy-blocked", "runner-blocked", "receiver-blocked"]
    assert (tmp_path / "runtime").stat().st_mode & 0o777 == 0o700
    assert all(
        path.stat().st_mode & 0o777 == 0o600 for path in (tmp_path / "runtime" / "owners").iterdir()
    )


def test_legacy_holder_excludes_runner_before_factory(tmp_path):
    runtime = tmp_path / "runtime"
    factory = Mock()
    runner = SessionRunner(runtime, TARGET, factory)
    with command_lock(runtime):
        with pytest.raises(RunnerUnavailable):
            runner.start()
        assert runner.shutdown(1).failure == RunnerFailure.OWNERSHIP
    factory.assert_not_called()


def test_cached_snapshots_remain_immutable_and_historical(owner):
    runner, _ = owner
    before = runner.snapshot()
    runner.start()
    after = runner.cancel()
    assert before.phase == RunnerPhase.NEW and before.cancellation_requested is False
    assert after.cancellation_requested
    with pytest.raises(FrozenInstanceError):
        attribute = "phase"
        setattr(after, attribute, RunnerPhase.NEW)


@pytest.mark.parametrize(
    "capacity,interval", [(0, 1), (True, 1), (1, 0), (1, True), (1, float("inf"))]
)
def test_invalid_configuration_has_no_side_effects(tmp_path, capacity, interval):
    with pytest.raises(ValueError):
        SessionRunner(
            tmp_path / "runtime", TARGET, Mock(), mailbox_capacity=capacity, step_interval=interval
        )
    assert not (tmp_path / "runtime").exists()


def test_shutdown_before_start_is_inert_and_final(tmp_path):
    runner = SessionRunner(tmp_path / "runtime", TARGET, Mock())
    assert runner.shutdown(0).phase == RunnerPhase.STOPPED
    assert not (tmp_path / "runtime").exists()
    with pytest.raises(RunnerUnavailable):
        runner.start()


def test_stop_identity_cannot_collide_with_active_start(owner):
    runner, tasks = owner
    runner.start()
    task = tasks[0]
    assert task.step_entered.wait(1)
    task.handle_release.clear()
    ticket = runner.submit(start_command("same-id"))
    try:
        assert task.handle_entered.wait(1)
        with pytest.raises(ValueError, match="different work"):
            runner.stop("same-id")
        assert runner.snapshot().cancellation_requested is False
        stop = runner.stop("different-stop-id")
        task.handle_release.set()
        assert ticket.wait(1).state == TicketState.HANDLED
        assert stop.wait(1).state == TicketState.HANDLED
        with pytest.raises(ValueError, match="different work"):
            runner.submit(RunnerCommand("different-stop-id", CommandAction.PAUSE))
    finally:
        task.handle_release.set()


def test_stop_identity_cannot_collide_with_pending_pause(tmp_path):
    made = []

    def factory(cancellation):
        task = Task(cancellation)
        task.step_release.clear()
        made.append(task)
        return task

    runner = SessionRunner(tmp_path / "runtime", TARGET, factory)
    try:
        runner.start()
        assert made[0].step_entered.wait(1)
        runner.submit(RunnerCommand("pause", CommandAction.PAUSE))
        with pytest.raises(ValueError, match="different work"):
            runner.stop("pause")
        stop = runner.stop("stop")
        with pytest.raises(ValueError, match="different work"):
            runner.stop("pause")
        assert runner.stop("distinct-stop-id") is stop
    finally:
        made[0].step_release.set()
        runner.shutdown(3)


def test_shutdown_discards_regular_commands_but_handles_already_requested_stop(tmp_path):
    made = []

    def factory(cancellation):
        task = Task(cancellation)
        task.step_release.clear()
        made.append(task)
        return task

    runner = SessionRunner(tmp_path / "runtime", TARGET, factory)
    try:
        runner.start()
        assert made[0].step_entered.wait(1)
        pause = runner.submit(RunnerCommand("pause", CommandAction.PAUSE))
        stop = runner.stop("stop")
        with pytest.raises(RunnerTimeout):
            runner.shutdown(0)
        assert pause.wait(0).state == TicketState.CANCELED
        made[0].step_release.set()
        assert runner.shutdown(1).phase == RunnerPhase.STOPPED
        assert stop.wait(0).state == TicketState.HANDLED
        assert made[0].effects == [CommandAction.STOP]
    finally:
        made[0].step_release.set()
        runner.shutdown(3)


def test_stop_latch_is_available_before_factory_returns(tmp_path):
    entered, release = Event(), Event()
    made = []

    def factory(cancellation):
        entered.set()
        assert release.wait(3)
        assert cancellation.requested
        task = Task(cancellation)
        made.append(task)
        return task

    runner = SessionRunner(tmp_path / "runtime", TARGET, factory)
    try:
        with pytest.raises(RunnerTimeout):
            runner.start(0)
        assert entered.wait(1)
        stop = runner.stop("stop")
        release.set()
        assert stop.wait(1).state == TicketState.HANDLED
        assert made[0].effects == [CommandAction.STOP]
    finally:
        release.set()
        runner.shutdown(3)


def test_failed_stop_coalesces_without_unbounded_retries(owner):
    runner, tasks = owner
    runner.start()
    assert tasks[0].step_entered.wait(1)
    tasks[0].failure = "handle"
    stop = runner.stop("first-stop")
    assert stop.wait(1).state == TicketState.FAILED
    assert runner.shutdown(1).phase == RunnerPhase.FAILED
    assert runner.stop("second-stop") is stop
    assert tasks[0].handled == [CommandAction.STOP]
