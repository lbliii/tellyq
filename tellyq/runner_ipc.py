"""Private, bounded local IPC over Unix sockets; no playback implementation lives here."""

import fcntl
import os
import socket
import stat
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Thread, current_thread
from time import monotonic
from typing import Protocol

from .models import IPCRequest, IPCResponse
from .runner import (
    CommandTicket,
    MailboxFull,
    RunnerCommand,
    RunnerPhase,
    RunnerTimeout,
    RunnerUnavailable,
    SessionRunner,
)
from .runner_codec import (
    MAX_FRAME_BYTES,
    IPCProtocolError,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
    error_response,
    fingerprint,
    wire_command,
    wire_snapshot,
    wire_ticket,
)


class IPCUnavailable(RuntimeError):
    """Local endpoint unavailable; clients must not fall back to another playback owner."""


def endpoint_path(runtime: Path) -> Path:
    return runtime / "runner.sock"


class FrameConnection(Protocol):
    def settimeout(self, timeout: float, /) -> None: ...
    def recv(self, count: int, /) -> bytes: ...


def read_frame(connection: FrameConnection, timeout: float) -> bytes:
    """Bound total frame time, including clients that trickle one byte at a time."""
    deadline = monotonic() + timeout
    data = bytearray()
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise IPCProtocolError("Frame deadline expired.")
        connection.settimeout(remaining)
        part = connection.recv(min(4096, MAX_FRAME_BYTES + 1 - len(data)))
        if not part:
            raise IPCProtocolError("Incomplete frame.")
        data.extend(part)
        if len(data) > MAX_FRAME_BYTES:
            raise IPCProtocolError("Frame is too large.")
        if b"\n" in part:
            if not data.endswith(b"\n") or b"\n" in data[:-1]:
                raise IPCProtocolError("Only one request is allowed per connection.")
            return bytes(data)


@dataclass(frozen=True, slots=True)
class _Entry:
    fingerprint: str
    ticket: CommandTicket


class RunnerIPCServer:
    """Explicit endpoint lifecycle around an already-active same-runtime owner.

    Fixed ticket capacity refuses new regular identities when full. It never
    silently evicts an identity and re-executes it. Stop has its own reserved slot.
    """

    def __init__(
        self,
        runtime: Path,
        runner: SessionRunner,
        *,
        max_clients: int = 8,
        ticket_capacity: int = 128,
        request_timeout: float = 1.0,
    ) -> None:
        if type(max_clients) is not int or not 2 <= max_clients <= 64:
            raise ValueError("Client capacity must be between two and 64.")
        if type(ticket_capacity) is not int or not 1 <= ticket_capacity <= 4096:
            raise ValueError("Ticket capacity must be between one and 4096.")
        if isinstance(request_timeout, bool) or not 0 < request_timeout <= 5:
            raise ValueError("Frame timeout must be positive and at most five seconds.")
        self.runtime, self.runner = runtime, runner
        self._max_clients, self._capacity, self._timeout = (
            max_clients,
            ticket_capacity,
            request_timeout,
        )
        self._tickets: OrderedDict[str, _Entry] = OrderedDict()
        self._stop: _Entry | None = None
        self._stop_aliases: dict[str, _Entry] = {}
        self._tickets_lock = Lock()
        self._lifecycle_lock = Lock()
        self._clients_lock = Lock()
        self._clients: dict[Thread, socket.socket] = {}
        self._listener: socket.socket | None = None
        self._thread: Thread | None = None
        self._lease: int | None = None
        self._identity: tuple[int, int] | None = None
        self._stopping = Event()
        self._started = False

    def handle_request(self, frame: bytes) -> IPCResponse:
        """Pure transport boundary for codec tests and the socket request handler."""
        try:
            request = decode_request(frame)
            if request.operation == "status":
                return {
                    "schema_version": 1,
                    "ok": True,
                    "code": "status",
                    "snapshot": wire_snapshot(self.runner.snapshot()),
                }
            if request.operation == "shutdown":
                try:
                    snapshot = self.runner.shutdown(request.timeout)
                except RunnerTimeout:
                    return {
                        **error_response("shutdown_timeout"),
                        "snapshot": wire_snapshot(self.runner.snapshot()),
                    }
                return {
                    "schema_version": 1,
                    "ok": True,
                    "code": "shutdown",
                    "snapshot": wire_snapshot(snapshot),
                }
            with self._tickets_lock:
                if request.operation == "ticket":
                    entry = self._find(request.command_id)
                    return (
                        self._ticket_response(entry, "ticket")
                        if entry
                        else error_response("ticket_missing")
                    )
                assert request.command is not None
                command = request.command
                mark = fingerprint(command)
                existing = self._find(command.command_id)
                if existing is not None:
                    if existing.fingerprint != mark:
                        return error_response("command_conflict")
                    return self._ticket_response(existing, "accepted")
                is_stop = command.action.value == "stop"
                if is_stop and self._stop is not None:
                    if len(self._stop_aliases) >= self._capacity:
                        return error_response("history_full")
                    self._stop_aliases[command.command_id] = _Entry(mark, self._stop.ticket)
                    return self._ticket_response(self._stop, "accepted")
                if not is_stop and len(self._tickets) >= self._capacity:
                    return error_response("history_full")
                ticket = self.runner.submit(command)
                entry = _Entry(mark, ticket)
                if is_stop:
                    # SessionRunner can coalesce a stop submitted directly by its
                    # host. Canonicalize to the actual ticket's command identity.
                    canonical = ticket.snapshot().command
                    if canonical.command_id in self._tickets:
                        return error_response("command_conflict")
                    entry = _Entry(fingerprint(canonical), ticket)
                    self._stop = entry
                    if command.command_id != canonical.command_id:
                        self._stop_aliases[command.command_id] = _Entry(mark, ticket)
                else:
                    self._tickets[command.command_id] = entry
                return self._ticket_response(entry, "accepted")
        except IPCProtocolError:
            return error_response("invalid_request")
        except MailboxFull:
            return error_response("mailbox_full")
        except RunnerUnavailable:
            return error_response("runner_unavailable")
        except ValueError:
            return error_response("command_rejected")
        except Exception:
            return error_response("internal_error")

    def _find(self, command_id: str | None) -> _Entry | None:
        if self._stop is not None and self._stop.ticket.snapshot().command.command_id == command_id:
            return self._stop
        if command_id is None:
            return None
        return self._stop_aliases.get(command_id) or self._tickets.get(command_id)

    @staticmethod
    def _ticket_response(entry: _Entry, code: str) -> IPCResponse:
        ticket = wire_ticket(entry.ticket.snapshot())
        return {
            "schema_version": 1,
            "ok": True,
            "code": "accepted" if code == "accepted" else "ticket",
            "ticket_id": ticket["command_id"],
            "ticket": ticket,
        }

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._started:
                raise IPCUnavailable("An endpoint instance can start only once.")
            snapshot = self.runner.snapshot()
            if snapshot.phase != RunnerPhase.RUNNING or not snapshot.owns_device:
                raise IPCUnavailable("An active exclusive runner is required before IPC starts.")
            if self.runtime.is_symlink() or not self.runtime.is_dir():
                raise IPCUnavailable("IPC requires the owner's private runtime directory.")
            if self.runtime.stat().st_uid != os.getuid():
                raise IPCUnavailable("IPC runtime belongs to another account.")
            self.runtime.chmod(0o700)
            path = endpoint_path(self.runtime)
            try:
                self._lease = os.open(
                    self.runtime / "ipc.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
                )
                if not stat.S_ISREG(os.fstat(self._lease).st_mode):
                    raise IPCUnavailable("IPC lease must be a regular private file.")
                os.fchmod(self._lease, 0o600)
                fcntl.flock(self._lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if path.is_symlink():
                    raise IPCUnavailable("IPC endpoint must not be a symlink.")
                if path.exists():
                    existing = path.lstat()
                    if not stat.S_ISSOCK(existing.st_mode):
                        raise IPCUnavailable("IPC endpoint is not a socket; it was left intact.")
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                        probe.settimeout(self._timeout)
                        try:
                            probe.connect(str(path))
                        except ConnectionRefusedError:
                            pass
                        else:
                            raise IPCUnavailable(
                                "An existing endpoint is live; it was left intact."
                            )
                    current = path.lstat()
                    if (current.st_dev, current.st_ino) != (existing.st_dev, existing.st_ino):
                        raise IPCUnavailable("IPC endpoint changed during validation.")
                    path.unlink()
                self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self._listener.bind(str(path))
                information = path.lstat()
                self._identity = (information.st_dev, information.st_ino)
                path.chmod(0o600)
                self._listener.listen(self._max_clients)
                self._listener.settimeout(0.1)
                self._thread = Thread(target=self._accept, name="tellyq-ipc", daemon=False)
                self._thread.start()
                self._started = True
            except Exception:
                self._cleanup_endpoint()
                raise IPCUnavailable(
                    "IPC endpoint could not start; existing unrelated paths were retained."
                ) from None

    def _accept(self) -> None:
        listener = self._listener
        assert listener is not None
        while not self._stopping.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with self._clients_lock:
                if len(self._clients) < self._max_clients:
                    thread = Thread(
                        target=self._serve,
                        args=(connection,),
                        name="tellyq-ipc-client",
                        daemon=False,
                    )
                    self._clients[thread] = connection
                    try:
                        thread.start()
                    except RuntimeError:
                        self._clients.pop(thread)
                    else:
                        continue
            # Saturation is explicit and bounded; do not queue unbounded threads.
            with connection:
                connection.settimeout(0.1)
                with suppress(OSError):
                    connection.sendall(encode_response(error_response("server_busy")))

    def _serve(self, connection: socket.socket) -> None:
        try:
            with connection:
                try:
                    frame = read_frame(connection, self._timeout)
                    response = self.handle_request(frame)
                    try:
                        encoded = encode_response(response)
                    except IPCProtocolError:
                        encoded = encode_response(error_response("response_too_large"))
                except IPCProtocolError, OSError:
                    encoded = encode_response(error_response("invalid_request"))
                connection.settimeout(self._timeout)
                connection.sendall(encoded)
        except OSError:
            pass
        finally:
            with self._clients_lock:
                self._clients.pop(current_thread(), None)

    def close(self, timeout: float = 2) -> None:
        if isinstance(timeout, bool) or not 0 <= timeout <= 10:
            raise ValueError("IPC close timeout must be between zero and ten seconds.")
        with self._lifecycle_lock:
            if self.runner.snapshot().owns_device:
                raise IPCUnavailable("Runner still owns its task; keep its endpoint available.")
            self._stopping.set()
            if self._listener is not None:
                self._listener.close()
            deadline = monotonic() + timeout
            if self._thread is not None:
                self._thread.join(max(0, deadline - monotonic()))
            with self._clients_lock:
                clients = tuple(self._clients.items())
            # A shutdown handler may have released runner ownership but still be
            # sending its final response. Give it the shared close budget before
            # interrupting any transport, or the client sees a false unknown result.
            for thread, _ in clients:
                thread.join(max(0, deadline - monotonic()))
            for thread, connection in clients:
                if thread.is_alive():
                    with suppress(OSError):
                        connection.shutdown(socket.SHUT_RDWR)
            if (self._thread is not None and self._thread.is_alive()) or any(
                thread.is_alive() for thread, _ in clients
            ):
                raise IPCUnavailable("IPC threads are still exiting; endpoint lease is retained.")
            self._cleanup_endpoint()

    def _cleanup_endpoint(self) -> None:
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        path = endpoint_path(self.runtime)
        if self._identity is not None:
            try:
                information = path.lstat()
                if (information.st_dev, information.st_ino) == self._identity and stat.S_ISSOCK(
                    information.st_mode
                ):
                    path.unlink()
            except FileNotFoundError:
                pass
            self._identity = None
        if self._lease is not None:
            os.close(self._lease)
            self._lease = None


class RunnerIPCClient:
    def __init__(self, runtime: Path, *, timeout: float = 2) -> None:
        if isinstance(timeout, bool) or not 0 < timeout <= 10:
            raise ValueError("Client timeout must be positive and at most ten seconds.")
        self.runtime, self.timeout = runtime, timeout

    def request(self, request: IPCRequest, *, timeout: float | None = None) -> IPCResponse:
        frame = encode_request(request)
        budget = self.timeout if timeout is None else timeout
        if isinstance(budget, bool) or not 0 < budget <= 15:
            raise ValueError("Request timeout must be positive and at most 15 seconds.")
        path = endpoint_path(self.runtime)
        try:
            information = path.lstat()
            directory = self.runtime.lstat()
            if (
                self.runtime.is_symlink()
                or not stat.S_ISDIR(directory.st_mode)
                or directory.st_uid != os.getuid()
                or directory.st_mode & 0o077
            ):
                raise IPCUnavailable("IPC directory is not private to this account.")
            if (
                not stat.S_ISSOCK(information.st_mode)
                or information.st_uid != os.getuid()
                or information.st_mode & 0o077
            ):
                raise IPCUnavailable("IPC endpoint is not a private socket.")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(budget)
                connection.connect(str(path))
                connection.sendall(frame)
                return decode_response(read_frame(connection, budget))
        except OSError, IPCProtocolError:
            raise IPCUnavailable(
                "Local owner request failed; outcome may be unknown. Do not fall back to direct playback."
            ) from None

    def status(self) -> IPCResponse:
        return self.request({"schema_version": 1, "operation": "status"})

    def submit(self, command: RunnerCommand) -> IPCResponse:
        return self.request(
            {"schema_version": 1, "operation": "submit", "command": wire_command(command)}
        )

    def ticket(self, command_id: str) -> IPCResponse:
        return self.request({"schema_version": 1, "operation": "ticket", "command_id": command_id})

    def shutdown(self, timeout: float = 0) -> IPCResponse:
        return self.request(
            {"schema_version": 1, "operation": "shutdown", "timeout": timeout},
            timeout=timeout + self.timeout,
        )
