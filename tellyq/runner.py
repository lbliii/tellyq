"""One foreground owner's thread, bounded mailbox and immutable cached read model.

This library does not compose playback, queue policy or IPC. A supplied task owns
bounded work and all device objects; command acceptance is never playback proof.
"""

import fcntl
import os
import stat
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from threading import Condition, Event, Lock, Thread, current_thread
from time import monotonic
from typing import Protocol

from .domain.queue import QueueSnapshot
from .domain.values import (
    CommandAction,
    PlaybackRequest,
    PlaybackTarget,
    SessionSnapshot,
    require_instant,
)


class RunnerPhase(StrEnum):
    NEW = "new"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class RunnerFailure(StrEnum):
    OWNERSHIP = "ownership_unavailable"
    INITIALIZATION = "task_initialization_failed"
    COMMAND = "command_failed"
    STEP = "observation_step_failed"
    CLOSE = "task_close_failed"


class TicketState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    HANDLED = "handled"
    CANCELED = "canceled"
    FAILED = "failed"


class RunnerUnavailable(RuntimeError):
    """The runner cannot accept work in its current phase."""


class MailboxFull(RunnerUnavailable):
    """The bounded regular mailbox has no capacity; nothing was submitted."""


class RunnerTimeout(TimeoutError):
    """A wait expired; the owner may still hold resources and its locks."""


@dataclass(frozen=True, slots=True)
class TaskView:
    playback: SessionSnapshot | None = None
    queue: QueueSnapshot | None = None


@dataclass(frozen=True, slots=True)
class RunnerCommand:
    command_id: str
    action: CommandAction
    request: PlaybackRequest | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.command_id, str) or not self.command_id.strip():
            raise ValueError("A command requires a nonempty identity.")
        if not isinstance(self.action, CommandAction):
            raise ValueError("Use an explicit supported command action.")
        if self.action == CommandAction.START and self.request is None:
            raise ValueError("Start requires an explicit playback request.")
        if self.action != CommandAction.START and self.request is not None:
            raise ValueError("Only start carries a playback request.")


@dataclass(frozen=True, slots=True)
class TicketSnapshot:
    command: RunnerCommand
    state: TicketState = TicketState.QUEUED
    view: TaskView | None = None
    failure: RunnerFailure | None = None


class CommandTicket:
    """A caller-owned bounded result handle; handled does not mean remote success."""

    def __init__(self, command: RunnerCommand) -> None:
        self._lock = Lock()
        self._done = Event()
        self._snapshot = TicketSnapshot(command)

    def snapshot(self) -> TicketSnapshot:
        with self._lock:
            return self._snapshot

    def wait(self, timeout: float) -> TicketSnapshot:
        _timeout(timeout)
        if not self._done.wait(timeout):
            raise RunnerTimeout("Command result is not yet available; do not resend blindly.")
        return self.snapshot()

    def _update(
        self,
        state: TicketState,
        *,
        view: TaskView | None = None,
        failure: RunnerFailure | None = None,
    ) -> None:
        with self._lock:
            self._snapshot = replace(self._snapshot, state=state, view=view, failure=failure)
            if state in {TicketState.HANDLED, TicketState.CANCELED, TicketState.FAILED}:
                self._done.set()


class Cancellation:
    """Read-only view of a permanent latch, shared with the task before construction."""

    def __init__(self, event: Event) -> None:
        self._event = event

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float) -> bool:
        _timeout(timeout)
        return self._event.wait(timeout)


class RunnerTask(Protocol):
    """All methods and construction run on the owner thread, with no mailbox lock.

    Bound each call's I/O. Step must remain observation-only once canceled; handle
    must check cancellation before start/resume effects, including after blocking
    preparation. Already dispatched remote operations cannot be retracted.
    Close releases resources; it must not infer an operator request to stop the TV.
    """

    def step(self, cancellation: Cancellation) -> TaskView: ...

    def handle(self, command: RunnerCommand, cancellation: Cancellation) -> TaskView: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class RunnerSnapshot:
    phase: RunnerPhase = RunnerPhase.NEW
    revision: int = 0
    owns_device: bool = False
    cancellation_requested: bool = False
    pending_commands: int = 0
    active_command: RunnerCommand | None = None
    last_command: TicketSnapshot | None = None
    view: TaskView = TaskView()
    failure: RunnerFailure | None = None


def _timeout(timeout: float) -> None:
    require_instant(timeout, "wait timeout")
    if timeout < 0:
        raise ValueError("Wait timeout must not be negative.")


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("Owner locks must be regular private files.")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


@contextmanager
def _ownership(runtime: Path, target: PlaybackTarget) -> Iterator[None]:
    if runtime.is_symlink():
        raise ValueError("Owner runtime must not be a symlink.")
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime.chmod(0o700)
    directory = runtime / "owners"
    if directory.is_symlink():
        raise ValueError("Owner lock directory must not be a symlink.")
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    identity = sha256(target.device_id.encode()).hexdigest()
    # Same inode/protocol as state.command_lock. Holding it for the lifetime
    # intentionally excludes every legacy CLI command and other runner here.
    with _file_lock(runtime / "command.lock"), _file_lock(directory / f"{identity}.lock"):
        yield


class SessionRunner:
    def __init__(
        self,
        runtime: Path,
        target: PlaybackTarget,
        task_factory: Callable[[Cancellation], RunnerTask],
        *,
        mailbox_capacity: int = 16,
        step_interval: float = 0.25,
    ) -> None:
        if type(mailbox_capacity) is not int or mailbox_capacity < 1:
            raise ValueError("Mailbox capacity must be a positive integer.")
        require_instant(step_interval, "step interval")
        if not 0 < step_interval <= 2:
            raise ValueError("Step interval must be positive and at most two seconds.")
        self._runtime, self._target, self._factory = runtime, target, task_factory
        self._capacity, self._interval = mailbox_capacity, step_interval
        self._condition = Condition()
        self._canceled = Event()
        self._cancellation = Cancellation(self._canceled)
        self._ready = Event()
        self._mailbox: deque[CommandTicket] = deque()
        self._stop: CommandTicket | None = None
        self._active: CommandTicket | None = None
        self._closing = False
        self._thread: Thread | None = None
        self._snapshot = RunnerSnapshot()

    def snapshot(self) -> RunnerSnapshot:
        """Read cached immutable state; never wait on a task, device or database."""
        with self._condition:
            return self._snapshot

    def _publish(self, **changes: object) -> None:
        # Called only while holding the condition; no external callbacks here.
        self._snapshot = replace(
            self._snapshot,
            revision=self._snapshot.revision + 1,
            cancellation_requested=self._canceled.is_set(),
            pending_commands=len(self._mailbox)
            + int(self._stop is not None and self._stop.snapshot().state == TicketState.QUEUED),
            **changes,
        )

    def start(self, timeout: float = 5) -> RunnerSnapshot:
        _timeout(timeout)
        with self._condition:
            if self._snapshot.phase != RunnerPhase.NEW:
                raise RunnerUnavailable("A runner can be started only once.")
            self._publish(phase=RunnerPhase.STARTING)
            self._thread = Thread(target=self._run, name="tellyq-owner", daemon=False)
            try:
                self._thread.start()
            except BaseException:
                self._publish(phase=RunnerPhase.FAILED, failure=RunnerFailure.INITIALIZATION)
                self._closing = True
                self._ready.set()
                raise
        if not self._ready.wait(timeout):
            raise RunnerTimeout(
                "Runner initialization is still in progress; ownership is retained."
            )
        result = self.snapshot()
        if result.phase != RunnerPhase.RUNNING:
            raise RunnerUnavailable("Runner could not start; inspect its cached failure status.")
        return result

    def _require_open(self) -> None:
        if self._closing or self._snapshot.phase not in {RunnerPhase.STARTING, RunnerPhase.RUNNING}:
            raise RunnerUnavailable("Runner is not accepting commands.")

    def submit(self, command: RunnerCommand) -> CommandTicket:
        if command.action == CommandAction.STOP:
            return self.stop(command.command_id)
        if command.request is not None and command.request.target != self._target:
            raise ValueError("Start targets a different receiver.")
        with self._condition:
            self._require_open()
            if self._canceled.is_set() and command.action in {
                CommandAction.START,
                CommandAction.RESUME,
            }:
                raise RunnerUnavailable("Starts and resumes are canceled for this runner lifetime.")
            existing = self._known_ticket(command)
            if existing is not None:
                return existing
            if len(self._mailbox) >= self._capacity:
                raise MailboxFull("Runner mailbox is full; no command was queued.")
            ticket = CommandTicket(command)
            self._mailbox.append(ticket)
            self._publish()
            self._condition.notify()
            return ticket

    def _known_ticket(self, command: RunnerCommand) -> CommandTicket | None:
        candidates = (
            *self._mailbox,
            *((self._active,) if self._active is not None else ()),
            *((self._stop,) if self._stop is not None else ()),
        )
        for ticket in candidates:
            existing = ticket.snapshot().command
            if existing.command_id == command.command_id:
                if existing != command:
                    raise ValueError("Known command identity describes different work.")
                return ticket
        return None

    def _cancel_pending(self, *, all_commands: bool = False) -> None:
        self._canceled.set()
        retained: deque[CommandTicket] = deque()
        for ticket in self._mailbox:
            if all_commands or ticket.snapshot().command.action in {
                CommandAction.START,
                CommandAction.RESUME,
            }:
                ticket._update(TicketState.CANCELED)
            else:
                retained.append(ticket)
        self._mailbox = retained

    def cancel(self) -> RunnerSnapshot:
        """Latch local cancellation immediately; send no remote stop by itself."""
        with self._condition:
            self._require_open()
            self._cancel_pending()
            self._publish()
            self._condition.notify()
            return self._snapshot

    def stop(self, command_id: str) -> CommandTicket:
        """Reserve one priority stop, coalescing all repeats for this lifetime."""
        command = RunnerCommand(command_id, CommandAction.STOP)
        with self._condition:
            existing = self._known_ticket(command)
            if existing is not None:
                return existing
            if self._stop is not None:
                return self._stop
            self._require_open()
            self._cancel_pending()
            self._stop = CommandTicket(command)
            self._publish()
            self._condition.notify()
            return self._stop

    def shutdown(self, timeout: float = 5) -> RunnerSnapshot:
        """Cancel pending work and join. A timeout never closes resources here."""
        _timeout(timeout)
        with self._condition:
            thread = self._thread
            if thread is current_thread():
                raise RunnerUnavailable("The owner thread cannot join itself.")
            self._closing = True
            self._cancel_pending(all_commands=True)
            if self._snapshot.phase == RunnerPhase.NEW:
                self._publish(phase=RunnerPhase.STOPPED)
            elif self._snapshot.phase not in {RunnerPhase.STOPPED, RunnerPhase.FAILED}:
                self._publish(phase=RunnerPhase.STOPPING)
            self._condition.notify()
        if thread is not None and thread.ident is not None:
            thread.join(timeout)
            if thread.is_alive():
                raise RunnerTimeout(
                    "Runner is still exiting; device ownership remains with its worker."
                )
        return self.snapshot()

    def _execute(self, task: RunnerTask) -> None:
        next_step = monotonic()
        while True:
            with self._condition:
                ticket: CommandTicket | None = None
                if self._stop is not None and self._stop.snapshot().state == TicketState.QUEUED:
                    ticket = self._stop
                elif self._closing:
                    return
                elif monotonic() >= next_step:
                    pass
                elif self._mailbox:
                    ticket = self._mailbox.popleft()
                else:
                    self._condition.wait(max(0, next_step - monotonic()))
                    continue
                if ticket is not None:
                    self._active = ticket
                    ticket._update(TicketState.RUNNING)
                    self._publish(active_command=ticket.snapshot().command)
            try:
                view = (
                    task.handle(ticket.snapshot().command, self._cancellation)
                    if ticket is not None
                    else task.step(self._cancellation)
                )
                if not isinstance(view, TaskView):
                    raise TypeError("Runner tasks must return an immutable TaskView.")
            except BaseException:
                with self._condition:
                    failure = RunnerFailure.COMMAND if ticket is not None else RunnerFailure.STEP
                    if ticket is not None:
                        ticket._update(TicketState.FAILED, failure=failure)
                    self._publish(
                        failure=failure,
                        last_command=ticket.snapshot() if ticket else self._snapshot.last_command,
                    )
                raise
            with self._condition:
                if ticket is not None:
                    ticket._update(TicketState.HANDLED, view=view)
                    self._active = None
                    self._publish(view=view, active_command=None, last_command=ticket.snapshot())
                else:
                    self._publish(view=view)
                    next_step = monotonic() + self._interval

    def _run(self) -> None:
        task: RunnerTask | None = None
        failure: RunnerFailure | None = None
        try:
            with _ownership(self._runtime, self._target):
                with self._condition:
                    self._publish(owns_device=True)
                try:
                    task = self._factory(self._cancellation)
                    with self._condition:
                        self._publish(
                            phase=RunnerPhase.STOPPING if self._closing else RunnerPhase.RUNNING
                        )
                        self._ready.set()
                    self._execute(task)
                except BaseException:
                    with self._condition:
                        failure = self._snapshot.failure or RunnerFailure.INITIALIZATION
                finally:
                    with self._condition:
                        self._closing = True
                        self._cancel_pending(all_commands=True)
                        if (
                            self._stop is not None
                            and self._stop.snapshot().state == TicketState.QUEUED
                        ):
                            self._stop._update(TicketState.CANCELED)
                        self._publish(
                            phase=RunnerPhase.STOPPING, active_command=None, failure=failure
                        )
                        self._ready.set()
                    if task is not None:
                        try:
                            task.close()
                        except BaseException:
                            failure = failure or RunnerFailure.CLOSE
        except BaseException:
            failure = failure or RunnerFailure.OWNERSHIP
        finally:
            with self._condition:
                self._closing = True
                self._cancel_pending(all_commands=True)
                if self._stop is not None and self._stop.snapshot().state == TicketState.QUEUED:
                    self._stop._update(TicketState.CANCELED)
                self._publish(
                    phase=RunnerPhase.FAILED if failure else RunnerPhase.STOPPED,
                    owns_device=False,
                    active_command=None,
                    failure=failure,
                )
                self._ready.set()
                self._condition.notify_all()
