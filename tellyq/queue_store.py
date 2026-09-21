"""Private SQLite queue intent and command journal.

Connections belong to one short operation, so threads never share a connection.
There is no device I/O, polling, automatic advancement or automatic command replay.
"""

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from .domain.native_queue import NativeQueueAction, NativeQueueRequest
from .domain.ports import RevisionConflict
from .domain.queue import (
    AttemptOrigin,
    ExecutionState,
    JournalCommand,
    NativeOperation,
    NativeReservation,
    NativeReservationState,
    QueueAttempt,
    QueueEntry,
    QueueIntent,
    QueueMode,
    QueueSnapshot,
)
from .domain.values import (
    CommandAction,
    ContentKind,
    ContentRef,
    IdentityUpdate,
    PlaybackTarget,
    PlayerState,
    SessionSnapshot,
    require_instant,
)
from .state import load_queue

_SCHEMA_VERSION = 2
_SCHEMA_V1 = (
    """CREATE TABLE queues (
        queue_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, route TEXT NOT NULL,
        name TEXT, revision INTEGER NOT NULL CHECK (revision >= 0),
        generation INTEGER NOT NULL CHECK (generation >= 0), current_item_id TEXT,
        cancellation_requested INTEGER NOT NULL CHECK (cancellation_requested IN (0, 1))
    ) STRICT""",
    """CREATE TABLE items (
        queue_id TEXT NOT NULL REFERENCES queues(queue_id), item_id TEXT NOT NULL,
        position INTEGER NOT NULL CHECK (position >= 0), provider TEXT NOT NULL,
        content_id TEXT NOT NULL, kind TEXT NOT NULL, title TEXT, intent TEXT NOT NULL,
        PRIMARY KEY (queue_id, item_id), UNIQUE (queue_id, position)
    ) STRICT""",
    """CREATE TABLE attempts (
        attempt_id TEXT PRIMARY KEY, queue_id TEXT NOT NULL, item_id TEXT NOT NULL,
        intent TEXT NOT NULL, created_at TEXT NOT NULL, decision_id TEXT UNIQUE,
        FOREIGN KEY (queue_id, item_id) REFERENCES items(queue_id, item_id)
    ) STRICT""",
    """CREATE TABLE commands (
        command_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
        action TEXT NOT NULL, state TEXT NOT NULL, generation INTEGER NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE imports (
        queue_id TEXT PRIMARY KEY REFERENCES queues(queue_id), digest TEXT NOT NULL
    ) STRICT""",
)
_NATIVE_SCHEMA = (
    """CREATE TABLE queue_options (
        queue_id TEXT PRIMARY KEY REFERENCES queues(queue_id), mode TEXT NOT NULL,
        reconciliation_required INTEGER NOT NULL CHECK (reconciliation_required IN (0, 1))
    ) STRICT""",
    """CREATE TABLE native_reservations (
        reservation_id TEXT PRIMARY KEY, queue_id TEXT NOT NULL REFERENCES queues(queue_id),
        predecessor_attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
        successor_item_id TEXT NOT NULL, successor_attempt_id TEXT NOT NULL UNIQUE,
        application_id TEXT NOT NULL, session_id TEXT NOT NULL, predecessor_playback_id TEXT NOT NULL,
        generation INTEGER NOT NULL CHECK (generation >= 0), created_at TEXT NOT NULL,
        state TEXT NOT NULL, adoption_decision_id TEXT UNIQUE, exit_decision_id TEXT UNIQUE,
        UNIQUE (queue_id, predecessor_attempt_id), UNIQUE (queue_id, successor_item_id),
        FOREIGN KEY (queue_id, successor_item_id) REFERENCES items(queue_id, item_id)
    ) STRICT""",
    """CREATE TABLE native_operations (
        operation_id TEXT PRIMARY KEY,
        reservation_id TEXT NOT NULL REFERENCES native_reservations(reservation_id),
        action TEXT NOT NULL, state TEXT NOT NULL, generation INTEGER NOT NULL CHECK (generation >= 0),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, scope_attempt_id TEXT NOT NULL,
        connection_generation TEXT NOT NULL, playback_id TEXT NOT NULL,
        UNIQUE (reservation_id, action)
    ) STRICT""",
    """CREATE TABLE native_attempt_origins (
        attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id),
        reservation_id TEXT NOT NULL UNIQUE REFERENCES native_reservations(reservation_id)
    ) STRICT""",
)
_SCHEMA = _SCHEMA_V1 + _NATIVE_SCHEMA
_SETTLED = frozenset(
    {QueueIntent.FINISHED, QueueIntent.SKIPPED, QueueIntent.STOPPED, QueueIntent.NEEDS_ATTENTION}
)


class QueueStoreError(ValueError):
    """Invalid or unsupported durable state; retain the original for recovery."""


def _identity(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("durable identities must be nonempty strings")


def _timestamp(at: datetime) -> str:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("journal timestamps must include a timezone")
    return at.isoformat()


def _revision(snapshot: QueueSnapshot, expected: int) -> None:
    if type(expected) is not int or snapshot.revision != expected:
        raise RevisionConflict("queue revision changed; reload before making a decision")


def _text(row: sqlite3.Row, field: str) -> str:
    value = row[field]
    if not isinstance(value, str) or not value:
        raise QueueStoreError("invalid durable string")
    return value


def _optional_text(row: sqlite3.Row, field: str) -> str | None:
    value = row[field]
    if value is None or isinstance(value, str):
        return value
    raise QueueStoreError("invalid optional durable string")


def _number(row: sqlite3.Row, field: str) -> int:
    value = row[field]
    if type(value) is not int or value < 0:
        raise QueueStoreError("invalid durable counter")
    return value


def _date(row: sqlite3.Row, field: str) -> datetime:
    value = datetime.fromisoformat(_text(row, field))
    _timestamp(value)
    return value


class SQLiteQueueStore:
    def __init__(self, runtime: Path) -> None:
        if runtime.is_symlink():
            raise QueueStoreError("runtime must be a private directory, not a symlink")
        runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
        runtime.chmod(0o700)
        self.path = runtime / "queue.sqlite3"
        try:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if self.path.is_symlink() or not self.path.is_file():
                raise QueueStoreError("queue database must be a regular private file") from None
        else:
            os.close(descriptor)
        self.path.chmod(0o600)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if version == 0 and not tables:
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            elif version == 1:
                self._validate_schema(connection, integrity=True, version=1)
                for row in connection.execute("SELECT queue_id FROM queues").fetchall():
                    self._load(connection, row[0], legacy=True)
                for statement in _NATIVE_SCHEMA:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO queue_options SELECT queue_id, 'legacy', 0 FROM queues"
                )
                connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            self._validate_schema(connection, integrity=True)
            connection.commit()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
        except sqlite3.Error as exc:
            raise QueueStoreError(
                "queue database is unavailable or invalid; retain it for recovery"
            ) from exc
        finally:
            if connection is not None:
                connection.close()  # An uncommitted transaction is rolled back.

    @staticmethod
    def _validate_schema(
        connection: sqlite3.Connection, *, integrity: bool = False, version: int = _SCHEMA_VERSION
    ) -> None:
        if connection.execute("PRAGMA user_version").fetchone()[0] != version:
            raise QueueStoreError("unsupported queue database version; retain it for recovery")
        objects = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema"
        ).fetchall()
        definitions = {row["name"]: row["sql"] for row in objects if row["type"] == "table"}
        expected = {
            statement.split()[2]: statement
            for statement in (_SCHEMA_V1 if version == 1 else _SCHEMA)
        }
        unexpected_objects = any(
            row["type"] != "table"
            and not (
                row["type"] == "index"
                and row["name"].startswith("sqlite_autoindex_")
                and row["tbl_name"] in expected
                and row["sql"] is None
            )
            for row in objects
        )
        if definitions != expected or unexpected_objects:
            raise QueueStoreError("unrecognized queue database schema; retain it for recovery")
        if integrity and connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise QueueStoreError("corrupt queue database; retain it for recovery")
        if integrity and connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise QueueStoreError("invalid queue references; retain it for recovery")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._validate_schema(connection)
            yield connection
            connection.commit()

    def load(self, queue_id: str) -> QueueSnapshot | None:
        _identity(queue_id)
        with self._connection() as connection:
            connection.execute("BEGIN")  # One consistent view across all tables.
            self._validate_schema(connection)
            return self._load(connection, queue_id)

    @staticmethod
    def _load(
        connection: sqlite3.Connection, queue_id: str, *, legacy: bool = False
    ) -> QueueSnapshot | None:
        row = connection.execute("SELECT * FROM queues WHERE queue_id=?", (queue_id,)).fetchone()
        if row is None:
            return None
        try:
            items = tuple(
                QueueEntry(
                    _text(item, "item_id"),
                    ContentRef(
                        _text(item, "provider"),
                        _text(item, "content_id"),
                        ContentKind(_text(item, "kind")),
                        _optional_text(item, "title"),
                    ),
                    QueueIntent(_text(item, "intent")),
                )
                for item in connection.execute(
                    "SELECT * FROM items WHERE queue_id=? ORDER BY position", (queue_id,)
                )
            )
            origins = (
                {}
                if legacy
                else {
                    _text(origin, "attempt_id"): _text(origin, "reservation_id")
                    for origin in connection.execute(
                        "SELECT native_attempt_origins.* FROM native_attempt_origins JOIN attempts USING (attempt_id) WHERE queue_id=?",
                        (queue_id,),
                    )
                }
            )
            attempts = tuple(
                QueueAttempt(
                    _text(attempt, "attempt_id"),
                    _text(attempt, "item_id"),
                    QueueIntent(_text(attempt, "intent")),
                    _date(attempt, "created_at"),
                    _optional_text(attempt, "decision_id"),
                    AttemptOrigin.NATIVE_OBSERVED
                    if attempt["attempt_id"] in origins
                    else AttemptOrigin.LOCAL_START,
                    origins.get(attempt["attempt_id"]),
                )
                for attempt in connection.execute(
                    "SELECT * FROM attempts WHERE queue_id=? ORDER BY rowid", (queue_id,)
                )
            )
            commands = tuple(
                JournalCommand(
                    _text(command, "command_id"),
                    _text(command, "attempt_id"),
                    CommandAction(_text(command, "action")),
                    ExecutionState(_text(command, "state")),
                    _number(command, "generation"),
                    _date(command, "created_at"),
                    _date(command, "updated_at"),
                )
                for command in connection.execute(
                    """SELECT commands.* FROM commands JOIN attempts USING (attempt_id)
                       WHERE attempts.queue_id=? ORDER BY commands.rowid""",
                    (queue_id,),
                )
            )
            canceled = _number(row, "cancellation_requested")
            if canceled not in (0, 1):
                raise QueueStoreError("invalid cancellation state")
            snapshot = QueueSnapshot(
                queue_id,
                PlaybackTarget(
                    _text(row, "device_id"), _text(row, "route"), _optional_text(row, "name")
                ),
                _number(row, "revision"),
                _number(row, "generation"),
                _optional_text(row, "current_item_id"),
                bool(canceled),
                items,
                attempts,
                commands,
            )
            if not legacy:
                snapshot = SQLiteQueueStore._native_snapshot(connection, snapshot)
            current = [item for item in items if item.intent == QueueIntent.CURRENT]
            if (
                not items
                or len(current) > 1
                or (current and current[0].item_id != snapshot.current_item_id)
                or (
                    snapshot.current_item_id is not None
                    and snapshot.current_item_id not in {item.item_id for item in items}
                )
            ):
                raise QueueStoreError("inconsistent queue position")
            item_intents = {item.item_id: item.intent for item in items}
            if (
                len({attempt.item_id for attempt in attempts}) != len(attempts)
                or (attempts and attempts[-1].item_id != snapshot.current_item_id)
                or any(
                    attempt.intent != item_intents[attempt.item_id]
                    or attempt.intent == QueueIntent.PENDING
                    or (attempt.intent == QueueIntent.CURRENT and attempt.decision_id is not None)
                    or sum(
                        command.attempt_id == attempt.attempt_id
                        and command.action == CommandAction.START
                        for command in commands
                    )
                    != (1 if attempt.origin == AttemptOrigin.LOCAL_START else 0)
                    for attempt in attempts
                )
                or any(
                    command.generation > snapshot.generation
                    or command.updated_at < command.created_at
                    for command in commands
                )
            ):
                raise QueueStoreError("inconsistent attempt or command history")
            return snapshot
        except (ValueError, KeyError, IndexError) as exc:
            raise QueueStoreError("invalid durable queue values; retain them for recovery") from exc

    @classmethod
    def _required(cls, connection: sqlite3.Connection, queue_id: str) -> QueueSnapshot:
        snapshot = cls._load(connection, queue_id)
        if snapshot is None:
            raise QueueStoreError("queue does not exist")
        return snapshot

    @classmethod
    def _changed(cls, connection: sqlite3.Connection, queue_id: str) -> QueueSnapshot:
        connection.execute("UPDATE queues SET revision=revision+1 WHERE queue_id=?", (queue_id,))
        return cls._required(connection, queue_id)

    @staticmethod
    def _insert(
        connection: sqlite3.Connection,
        queue_id: str,
        target: PlaybackTarget,
        items: tuple[QueueEntry, ...],
        *,
        imported: bool = False,
        mode: QueueMode = QueueMode.LEGACY,
    ) -> None:
        _identity(queue_id)
        if not isinstance(mode, QueueMode):
            raise ValueError("queue mode must be explicit")
        if not isinstance(items, tuple) or not items:
            raise ValueError("a queue needs an immutable, nonempty tuple of items")
        if len({item.item_id for item in items}) != len(items):
            raise ValueError("queue item IDs must be unique")
        if not imported and any(item.intent != QueueIntent.PENDING for item in items):
            raise ValueError("new queue items must be pending")
        connection.execute(
            "INSERT INTO queues VALUES (?, ?, ?, ?, 0, 0, NULL, ?)",
            (
                queue_id,
                target.device_id,
                target.route,
                target.name,
                int(any(item.intent == QueueIntent.NEEDS_ATTENTION for item in items)),
            ),
        )
        connection.execute("INSERT INTO queue_options VALUES (?, ?, 0)", (queue_id, mode.value))
        connection.executemany(
            "INSERT INTO items VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    queue_id,
                    item.item_id,
                    index,
                    item.content.provider,
                    item.content.content_id,
                    item.content.kind.value,
                    item.content.title,
                    item.intent.value,
                )
                for index, item in enumerate(items)
            ],
        )

    def create(
        self,
        queue_id: str,
        target: PlaybackTarget,
        items: tuple[QueueEntry, ...],
        *,
        mode: QueueMode = QueueMode.LEGACY,
    ) -> QueueSnapshot:
        with self._transaction() as connection:
            if self._load(connection, queue_id) is not None:
                raise RevisionConflict("queue already exists")
            self._insert(connection, queue_id, target, items, mode=mode)
            return self._required(connection, queue_id)

    @staticmethod
    def _duplicate(
        snapshot: QueueSnapshot, command_id: str, attempt_id: str, action: CommandAction
    ) -> bool:
        for command in snapshot.commands:
            if command.command_id == command_id:
                if command.attempt_id != attempt_id or command.action != action:
                    raise QueueStoreError("command identity already describes another intent")
                return True
        return False

    @staticmethod
    def _command(
        connection: sqlite3.Connection,
        snapshot: QueueSnapshot,
        attempt_id: str,
        command_id: str,
        action: CommandAction,
        timestamp: str,
    ) -> None:
        connection.execute(
            "INSERT INTO commands VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                command_id,
                attempt_id,
                action.value,
                ExecutionState.PENDING.value,
                snapshot.generation,
                timestamp,
                timestamp,
            ),
        )

    def prepare_start(
        self,
        queue_id: str,
        item_id: str,
        *,
        attempt_id: str,
        command_id: str,
        at: datetime,
        expected_revision: int,
    ) -> QueueSnapshot:
        for identity in (queue_id, item_id, attempt_id, command_id):
            _identity(identity)
        timestamp = _timestamp(at)
        with self._transaction() as connection:
            snapshot = self._required(connection, queue_id)
            if self._duplicate(snapshot, command_id, attempt_id, CommandAction.START):
                if (
                    next(a for a in snapshot.attempts if a.attempt_id == attempt_id).item_id
                    != item_id
                ):
                    raise QueueStoreError("command identity already describes another item")
                return snapshot
            _revision(snapshot, expected_revision)
            if snapshot.mode == QueueMode.NATIVE and (
                snapshot.attempts or snapshot.reconciliation_required
            ):
                raise QueueStoreError("native successors are observed, never started again")
            if snapshot.cancellation_requested or any(
                item.intent in {QueueIntent.CURRENT, QueueIntent.NEEDS_ATTENTION}
                for item in snapshot.items
            ):
                raise QueueStoreError("queue is canceled or requires reconciliation")
            if not any(
                item.item_id == item_id and item.intent == QueueIntent.PENDING
                for item in snapshot.items
            ):
                raise QueueStoreError("only an explicitly selected pending item can start")
            connection.execute(
                "UPDATE items SET intent=? WHERE queue_id=? AND item_id=?",
                (QueueIntent.CURRENT.value, queue_id, item_id),
            )
            connection.execute(
                "UPDATE queues SET current_item_id=? WHERE queue_id=?", (item_id, queue_id)
            )
            connection.execute(
                "INSERT INTO attempts VALUES (?, ?, ?, ?, ?, NULL)",
                (attempt_id, queue_id, item_id, QueueIntent.CURRENT.value, timestamp),
            )
            self._command(
                connection, snapshot, attempt_id, command_id, CommandAction.START, timestamp
            )
            return self._changed(connection, queue_id)

    def prepare_stop(
        self,
        queue_id: str,
        *,
        attempt_id: str,
        command_id: str,
        at: datetime,
        expected_revision: int,
    ) -> QueueSnapshot:
        _identity(command_id)
        timestamp = _timestamp(at)
        with self._transaction() as connection:
            snapshot = self._required(connection, queue_id)
            if self._duplicate(snapshot, command_id, attempt_id, CommandAction.STOP):
                return snapshot
            _revision(snapshot, expected_revision)
            if not any(
                a.attempt_id == attempt_id
                and a.item_id == snapshot.current_item_id
                and a.intent in {QueueIntent.CURRENT, QueueIntent.FINISHED}
                for a in snapshot.attempts
            ):
                raise QueueStoreError("stop requires an unresolved attempt")
            connection.execute(
                "UPDATE queues SET cancellation_requested=1 WHERE queue_id=?", (queue_id,)
            )
            self._command(
                connection, snapshot, attempt_id, command_id, CommandAction.STOP, timestamp
            )
            return self._changed(connection, queue_id)

    def prepare_control(
        self,
        queue_id: str,
        *,
        attempt_id: str,
        command_id: str,
        action: CommandAction,
        at: datetime,
        expected_revision: int,
    ) -> QueueSnapshot:
        if action not in {CommandAction.PAUSE, CommandAction.RESUME, CommandAction.RELEASE}:
            raise ValueError("control must be pause, resume or release")
        _identity(command_id)
        timestamp = _timestamp(at)
        with self._transaction() as connection:
            snapshot = self._required(connection, queue_id)
            if self._duplicate(snapshot, command_id, attempt_id, action):
                return snapshot
            _revision(snapshot, expected_revision)
            if action == CommandAction.RELEASE and any(
                reservation.state == NativeReservationState.RESERVED
                for reservation in snapshot.reservations
            ):
                raise QueueStoreError(
                    "native successor may execute; release fallback is prohibited"
                )
            expected = (
                QueueIntent.FINISHED if action == CommandAction.RELEASE else QueueIntent.CURRENT
            )
            if snapshot.cancellation_requested or not snapshot.attempts:
                raise QueueStoreError("control requires an uncanceled current owner")
            attempt = snapshot.attempts[-1]
            if (
                attempt.attempt_id != attempt_id
                or attempt.item_id != snapshot.current_item_id
                or attempt.intent != expected
                or (action == CommandAction.RELEASE and not attempt.decision_id)
                or any(
                    c.state
                    in {ExecutionState.PENDING, ExecutionState.DISPATCHED, ExecutionState.UNCERTAIN}
                    for c in snapshot.commands
                )
            ):
                raise QueueStoreError("control requires the resolved latest attempt")
            if action == CommandAction.RELEASE and any(
                c.attempt_id == attempt_id and c.action == CommandAction.RELEASE
                for c in snapshot.commands
            ):
                raise QueueStoreError(
                    "release was already attempted; reconcile instead of resending"
                )
            self._command(connection, snapshot, attempt_id, command_id, action, timestamp)
            return self._changed(connection, queue_id)

    def prepare_release(
        self,
        queue_id: str,
        *,
        attempt_id: str,
        command_id: str,
        at: datetime,
        expected_revision: int,
    ) -> QueueSnapshot:
        return self.prepare_control(
            queue_id,
            attempt_id=attempt_id,
            command_id=command_id,
            action=CommandAction.RELEASE,
            at=at,
            expected_revision=expected_revision,
        )

    def cancel_queue(self, queue_id: str, *, expected_revision: int) -> QueueSnapshot:
        """Persist local cancellation even before an attempt or between items."""
        with self._transaction() as connection:
            snapshot = self._required(connection, queue_id)
            _revision(snapshot, expected_revision)
            if snapshot.cancellation_requested:
                return snapshot
            connection.execute(
                "UPDATE queues SET cancellation_requested=1 WHERE queue_id=?", (queue_id,)
            )
            return self._changed(connection, queue_id)

    @staticmethod
    def _find_command(snapshot: QueueSnapshot, command_id: str) -> JournalCommand:
        for command in snapshot.commands:
            if command.command_id == command_id:
                return command
        raise QueueStoreError("command does not exist in this queue")

    def mark_dispatched(
        self, queue_id: str, command_id: str, *, at: datetime, expected_revision: int
    ) -> QueueSnapshot:
        return self._transition(
            queue_id, command_id, ExecutionState.DISPATCHED, at, expected_revision
        )

    def record_outcome(
        self,
        queue_id: str,
        command_id: str,
        outcome: ExecutionState,
        *,
        at: datetime,
        expected_revision: int,
    ) -> QueueSnapshot:
        if outcome not in {
            ExecutionState.ACKNOWLEDGED,
            ExecutionState.REJECTED,
            ExecutionState.UNCERTAIN,
        }:
            raise ValueError("a command outcome must be acknowledged, rejected or uncertain")
        return self._transition(queue_id, command_id, outcome, at, expected_revision)

    def _transition(
        self,
        queue_id: str,
        command_id: str,
        state: ExecutionState,
        at: datetime,
        expected_revision: int,
    ) -> QueueSnapshot:
        timestamp = _timestamp(at)
        with self._transaction() as connection:
            snapshot = self._required(connection, queue_id)
            command = self._find_command(snapshot, command_id)
            _revision(snapshot, expected_revision)
            if command.generation != snapshot.generation:
                raise RevisionConflict("command belongs to an earlier runner generation")
            if command.state == state and state != ExecutionState.DISPATCHED:
                return snapshot
            if state == ExecutionState.DISPATCHED:
                if command.state != ExecutionState.PENDING:
                    raise QueueStoreError(
                        "an uncertain or completed command cannot be redispatched"
                    )
                if command.action != CommandAction.STOP and snapshot.cancellation_requested:
                    raise QueueStoreError("start was canceled before dispatch")
                attempt = next(a for a in snapshot.attempts if a.attempt_id == command.attempt_id)
                allowed = {QueueIntent.CURRENT}
                if command.action in {CommandAction.RELEASE, CommandAction.STOP}:
                    allowed.add(QueueIntent.FINISHED)
                if command.action == CommandAction.RELEASE:
                    allowed = {QueueIntent.FINISHED}
                if (
                    attempt != snapshot.attempts[-1]
                    or attempt.item_id != snapshot.current_item_id
                    or attempt.intent not in allowed
                    or not any(
                        item.item_id == attempt.item_id and item.intent == attempt.intent
                        for item in snapshot.items
                    )
                ):
                    raise QueueStoreError("dispatch requires a current unresolved attempt")
            elif command.state not in {ExecutionState.DISPATCHED, ExecutionState.UNCERTAIN}:
                raise QueueStoreError("only a dispatched command can receive an outcome")
            if at < command.updated_at:
                raise ValueError("journal timestamps cannot move backward")
            connection.execute(
                "UPDATE commands SET state=?, updated_at=? WHERE command_id=?",
                (state.value, timestamp, command_id),
            )
            return self._changed(connection, queue_id)

    def settle_attempt(
        self,
        queue_id: str,
        attempt_id: str,
        outcome: QueueIntent,
        *,
        decision_id: str,
        expected_revision: int,
    ) -> QueueSnapshot:
        _identity(decision_id)
        if outcome not in _SETTLED:
            raise ValueError("settlement requires an explicit terminal or attention decision")
        with self._transaction() as connection:
            snapshot = self._required(connection, queue_id)
            attempt = next((a for a in snapshot.attempts if a.attempt_id == attempt_id), None)
            if attempt is None:
                raise QueueStoreError("attempt does not exist in this queue")
            if attempt.decision_id == decision_id and attempt.intent == outcome:
                return snapshot
            _revision(snapshot, expected_revision)
            if attempt.decision_id is not None or attempt.intent not in {
                QueueIntent.CURRENT,
                QueueIntent.NEEDS_ATTENTION,
            }:
                raise QueueStoreError("attempt already has a terminal decision")
            if outcome == QueueIntent.FINISHED and any(
                c.attempt_id == attempt_id and c.action == CommandAction.STOP
                for c in snapshot.commands
            ):
                raise QueueStoreError("a stop intent cannot become natural completion")
            connection.execute(
                "UPDATE attempts SET intent=?, decision_id=? WHERE attempt_id=?",
                (outcome.value, decision_id, attempt_id),
            )
            connection.execute(
                "UPDATE items SET intent=? WHERE queue_id=? AND item_id=?",
                (outcome.value, queue_id, attempt.item_id),
            )
            if outcome in {QueueIntent.STOPPED, QueueIntent.NEEDS_ATTENTION}:
                if snapshot.mode == QueueMode.NATIVE and outcome == QueueIntent.NEEDS_ATTENTION:
                    connection.execute(
                        "UPDATE queue_options SET reconciliation_required=1 WHERE queue_id=?",
                        (queue_id,),
                    )
                else:
                    connection.execute(
                        "UPDATE queues SET cancellation_requested=1 WHERE queue_id=?", (queue_id,)
                    )
            return self._changed(connection, queue_id)

    def recover(self, queue_id: str, *, expected_revision: int) -> QueueSnapshot:
        """Exclusive owner only: invalidate old workers and require fresh reconciliation.

        Pending means not dispatched by this journal, but is never automatically
        replayed. Dispatched means the remote effect may have happened. Accepted
        receipts remain history; neither establishes current playback ownership.
        """
        with self._transaction() as connection:
            snapshot = self._required(connection, queue_id)
            _revision(snapshot, expected_revision)
            if snapshot.mode == QueueMode.NATIVE:
                if not snapshot.attempts and not snapshot.reservations:
                    return snapshot
                connection.execute(
                    "UPDATE queues SET generation=generation+1 WHERE queue_id=?", (queue_id,)
                )
                connection.execute(
                    "UPDATE queue_options SET reconciliation_required=1 WHERE queue_id=?",
                    (queue_id,),
                )
                connection.execute(
                    "UPDATE commands SET state=? WHERE state=? AND attempt_id IN (SELECT attempt_id FROM attempts WHERE queue_id=?)",
                    (ExecutionState.UNCERTAIN.value, ExecutionState.DISPATCHED.value, queue_id),
                )
                connection.execute(
                    "UPDATE native_operations SET state=? WHERE state=? AND reservation_id IN (SELECT reservation_id FROM native_reservations WHERE queue_id=?)",
                    (ExecutionState.UNCERTAIN.value, ExecutionState.DISPATCHED.value, queue_id),
                )
                return self._changed(connection, queue_id)
            if not any(a.intent == QueueIntent.CURRENT for a in snapshot.attempts) and not any(
                c.generation == snapshot.generation
                and c.state
                in {ExecutionState.PENDING, ExecutionState.DISPATCHED, ExecutionState.UNCERTAIN}
                for c in snapshot.commands
            ):
                return snapshot
            connection.execute(
                "UPDATE queues SET generation=generation+1, cancellation_requested=1 WHERE queue_id=?",
                (queue_id,),
            )
            connection.execute(
                "UPDATE items SET intent=? WHERE queue_id=? AND intent=?",
                (QueueIntent.NEEDS_ATTENTION.value, queue_id, QueueIntent.CURRENT.value),
            )
            connection.execute(
                "UPDATE attempts SET intent=? WHERE queue_id=? AND intent=?",
                (QueueIntent.NEEDS_ATTENTION.value, queue_id, QueueIntent.CURRENT.value),
            )
            connection.execute(
                """UPDATE commands SET state=? WHERE state=? AND attempt_id IN
                   (SELECT attempt_id FROM attempts WHERE queue_id=?)""",
                (ExecutionState.UNCERTAIN.value, ExecutionState.DISPATCHED.value, queue_id),
            )
            return self._changed(connection, queue_id)

    def import_legacy(self, runtime: Path, *, queue_id: str) -> QueueSnapshot:
        """Explicit strict v1 import. Leave JSON and legacy session ownership untouched."""
        legacy = load_queue(runtime)
        digest = hashlib.sha256(json.dumps(legacy, sort_keys=True).encode()).hexdigest()
        item = legacy["items"][0]
        intent = {
            "queued": QueueIntent.PENDING,
            "stopped": QueueIntent.STOPPED,
            "finished": QueueIntent.FINISHED,
        }.get(item["state"], QueueIntent.NEEDS_ATTENTION)
        entry = QueueEntry(
            item["id"], ContentRef(item["service"], item["content_id"], title=item["title"]), intent
        )
        with self._transaction() as connection:
            imported = connection.execute(
                "SELECT digest FROM imports WHERE queue_id=?", (queue_id,)
            ).fetchone()
            if imported is not None:
                if imported[0] != digest:
                    raise QueueStoreError(
                        "legacy source changed since this import; retain both versions"
                    )
                return self._required(connection, queue_id)
            if self._load(connection, queue_id) is not None:
                raise RevisionConflict("import must use a new queue identity")
            self._insert(
                connection,
                queue_id,
                PlaybackTarget(legacy["device_id"], "cast"),
                (entry,),
                imported=True,
            )
            connection.execute("INSERT INTO imports VALUES (?, ?)", (queue_id, digest))
            return self._required(connection, queue_id)

    @staticmethod
    def _native_snapshot(connection: sqlite3.Connection, snapshot: QueueSnapshot) -> QueueSnapshot:
        options = connection.execute(
            "SELECT * FROM queue_options WHERE queue_id=?", (snapshot.queue_id,)
        ).fetchone()
        if options is None:
            raise QueueStoreError("missing queue options")
        hold = _number(options, "reconciliation_required")
        if hold not in (0, 1):
            raise QueueStoreError("invalid reconciliation hold")
        reservations = tuple(
            NativeReservation(
                _text(row, "reservation_id"),
                _text(row, "predecessor_attempt_id"),
                _text(row, "successor_item_id"),
                _text(row, "successor_attempt_id"),
                _text(row, "application_id"),
                _text(row, "session_id"),
                _text(row, "predecessor_playback_id"),
                _number(row, "generation"),
                _date(row, "created_at"),
                NativeReservationState(_text(row, "state")),
                _optional_text(row, "adoption_decision_id"),
                _optional_text(row, "exit_decision_id"),
            )
            for row in connection.execute(
                "SELECT * FROM native_reservations WHERE queue_id=? ORDER BY rowid",
                (snapshot.queue_id,),
            )
        )
        operations = tuple(
            NativeOperation(
                _text(row, "operation_id"),
                _text(row, "reservation_id"),
                NativeQueueAction(_text(row, "action")),
                ExecutionState(_text(row, "state")),
                _number(row, "generation"),
                _date(row, "created_at"),
                _date(row, "updated_at"),
                _text(row, "scope_attempt_id"),
                _text(row, "connection_generation"),
                _text(row, "playback_id"),
            )
            for row in connection.execute(
                "SELECT native_operations.* FROM native_operations JOIN native_reservations USING (reservation_id) WHERE queue_id=? ORDER BY native_operations.rowid",
                (snapshot.queue_id,),
            )
        )
        result = replace(
            snapshot,
            mode=QueueMode(_text(options, "mode")),
            reconciliation_required=bool(hold),
            reservations=reservations,
            native_operations=operations,
        )
        SQLiteQueueStore._validate_native_history(result)
        return result

    @staticmethod
    def _validate_native_history(snapshot: QueueSnapshot) -> None:
        if snapshot.mode == QueueMode.LEGACY:
            if (
                snapshot.reservations
                or snapshot.native_operations
                or snapshot.reconciliation_required
                or any(a.origin != AttemptOrigin.LOCAL_START for a in snapshot.attempts)
            ):
                raise QueueStoreError("legacy queue contains native history")
            return
        attempts = {a.attempt_id: a for a in snapshot.attempts}
        items = {item.item_id: item for item in snapshot.items}
        positions = {item.item_id: i for i, item in enumerate(snapshot.items)}
        if sum(r.state == NativeReservationState.RESERVED for r in snapshot.reservations) > 1:
            raise QueueStoreError("more than one native successor is outstanding")
        for reservation in snapshot.reservations:
            source = attempts.get(reservation.predecessor_attempt_id)
            successor = attempts.get(reservation.successor_attempt_id)
            operations = [
                o
                for o in snapshot.native_operations
                if o.reservation_id == reservation.reservation_id
            ]
            enqueue = [o for o in operations if o.action == NativeQueueAction.PLAY_NEXT]
            if (
                source is None
                or reservation.generation > snapshot.generation
                or reservation.successor_item_id not in items
                or positions[reservation.successor_item_id] != positions[source.item_id] + 1
                or items[source.item_id].content == items[reservation.successor_item_id].content
                or len(enqueue) != 1
                or enqueue[0].scope_attempt_id != source.attempt_id
                or enqueue[0].generation != reservation.generation
                or enqueue[0].playback_id != reservation.predecessor_playback_id
                or len({o.action for o in operations}) != len(operations)
                or any(
                    o.generation > snapshot.generation
                    or o.generation < reservation.generation
                    or o.updated_at < o.created_at
                    or o.created_at < reservation.created_at
                    or o.scope_attempt_id
                    not in {source.attempt_id, reservation.successor_attempt_id}
                    for o in operations
                )
                or (
                    reservation.state == NativeReservationState.RESERVED
                    and (
                        successor is not None
                        or reservation.adoption_decision_id is not None
                        or items[reservation.successor_item_id].intent != QueueIntent.PENDING
                        or snapshot.current_item_id != source.item_id
                    )
                )
                or (
                    reservation.state == NativeReservationState.ADOPTED
                    and reservation.adoption_decision_id is None
                )
                or (
                    (reservation.state == NativeReservationState.SESSION_EXIT_OBSERVED)
                    != (reservation.exit_decision_id is not None)
                )
                or (
                    reservation.adoption_decision_id is not None
                    and (
                        successor is None
                        or successor.item_id != reservation.successor_item_id
                        or successor.origin != AttemptOrigin.NATIVE_OBSERVED
                        or successor.reservation_id != reservation.reservation_id
                        or enqueue[0].state
                        not in {
                            ExecutionState.DISPATCHED,
                            ExecutionState.ACKNOWLEDGED,
                            ExecutionState.UNCERTAIN,
                        }
                    )
                )
                or (reservation.adoption_decision_id is None and successor is not None)
            ):
                raise QueueStoreError("inconsistent native reservation history")
        for attempt in snapshot.attempts:
            if attempt.origin == AttemptOrigin.NATIVE_OBSERVED and not any(
                r.reservation_id == attempt.reservation_id
                and r.successor_attempt_id == attempt.attempt_id
                and r.adoption_decision_id is not None
                for r in snapshot.reservations
            ):
                raise QueueStoreError("native attempt lacks observed reservation provenance")

    @staticmethod
    def _native_fence(snapshot: QueueSnapshot, revision: int, generation: int) -> None:
        if snapshot.mode != QueueMode.NATIVE:
            raise QueueStoreError("native queue mode is required")
        if type(generation) is not int or generation != snapshot.generation:
            raise RevisionConflict("native owner generation changed")
        _revision(snapshot, revision)

    @staticmethod
    def _reservation(snapshot: QueueSnapshot, reservation_id: str) -> NativeReservation:
        reservation = next(
            (r for r in snapshot.reservations if r.reservation_id == reservation_id), None
        )
        if reservation is None:
            raise QueueStoreError("native reservation does not exist in this queue")
        return reservation

    @staticmethod
    def _native_operation(snapshot: QueueSnapshot, operation_id: str) -> NativeOperation:
        operation = next(
            (o for o in snapshot.native_operations if o.operation_id == operation_id), None
        )
        if operation is None:
            raise QueueStoreError("native operation does not exist in this queue")
        return operation

    @staticmethod
    def _native_request_matches(
        snapshot: QueueSnapshot, reservation: NativeReservation, request: NativeQueueRequest
    ) -> None:
        scope, current = request.scope, request.scope.request
        source = next(
            a for a in snapshot.attempts if a.attempt_id == reservation.predecessor_attempt_id
        )
        expected_item = (
            source.item_id
            if current.attempt_id == source.attempt_id
            else reservation.successor_item_id
        )
        item = next(i for i in snapshot.items if i.item_id == expected_item)
        if (
            current.attempt_id not in {source.attempt_id, reservation.successor_attempt_id}
            or current.queue_item_id != item.item_id
            or current.content != item.content
            or current.target != snapshot.target
            or scope.application_id != reservation.application_id
            or scope.session_id != reservation.session_id
        ):
            raise QueueStoreError("native operation scope differs from its reservation")
        if request.action == NativeQueueAction.PLAY_NEXT and (
            current.attempt_id != source.attempt_id
            or request.playback_id != reservation.predecessor_playback_id
            or request.successor
            != next(i.content for i in snapshot.items if i.item_id == reservation.successor_item_id)
        ):
            raise QueueStoreError("native successor request differs from its reservation")

    @staticmethod
    def _insert_native_operation(
        connection: sqlite3.Connection,
        snapshot: QueueSnapshot,
        reservation_id: str,
        request: NativeQueueRequest,
        timestamp: str,
    ) -> None:
        connection.execute(
            "INSERT INTO native_operations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request.operation_id,
                reservation_id,
                request.action.value,
                ExecutionState.PENDING.value,
                snapshot.generation,
                timestamp,
                timestamp,
                request.scope.request.attempt_id,
                request.scope.connection_generation,
                request.playback_id,
            ),
        )

    def hold_for_reconciliation(self, queue_id: str, *, expected_revision: int) -> QueueSnapshot:
        with self._transaction() as connection:
            snapshot = self._required(connection, queue_id)
            self._native_fence(snapshot, expected_revision, snapshot.generation)
            if snapshot.reconciliation_required:
                return snapshot
            connection.execute(
                "UPDATE queue_options SET reconciliation_required=1 WHERE queue_id=?", (queue_id,)
            )
            return self._changed(connection, queue_id)

    def prepare_native_successor(
        self,
        queue_id: str,
        successor_item_id: str,
        *,
        reservation_id: str,
        successor_attempt_id: str,
        request: NativeQueueRequest,
        at: datetime,
        expected_revision: int,
        expected_generation: int,
    ) -> QueueSnapshot:
        for identity in (
            reservation_id,
            successor_item_id,
            successor_attempt_id,
            request.operation_id,
        ):
            _identity(identity)
        if request.action != NativeQueueAction.PLAY_NEXT or request.scope.application_id is None:
            raise ValueError("native staging requires play_next and explicit receiver scope")
        timestamp = _timestamp(at)
        with self._transaction() as connection:
            snapshot = self._required(connection, queue_id)
            existing = next(
                (o for o in snapshot.native_operations if o.operation_id == request.operation_id),
                None,
            )
            if existing is not None:
                reservation = self._reservation(snapshot, reservation_id)
                self._native_request_matches(snapshot, reservation, request)
                if (
                    existing.reservation_id != reservation_id
                    or existing.action != request.action
                    or reservation.successor_item_id != successor_item_id
                    or reservation.successor_attempt_id != successor_attempt_id
                    or existing.connection_generation != request.scope.connection_generation
                    or type(expected_generation) is not int
                    or existing.generation != expected_generation
                    or snapshot.generation != expected_generation
                ):
                    raise RevisionConflict("native operation identity or generation differs")
                return snapshot
            self._native_fence(snapshot, expected_revision, expected_generation)
            if (
                snapshot.cancellation_requested
                or snapshot.reconciliation_required
                or any(r.state == NativeReservationState.RESERVED for r in snapshot.reservations)
            ):
                raise QueueStoreError("native queue is canceled, held or already reserved")
            if not snapshot.attempts:
                raise QueueStoreError("native staging requires a current predecessor")
            source = snapshot.attempts[-1]
            if at < source.created_at:
                raise ValueError("journal timestamps cannot move backward")
            index = next(
                i for i, item in enumerate(snapshot.items) if item.item_id == source.item_id
            )
            if (
                source.intent != QueueIntent.CURRENT
                or source.attempt_id != request.scope.request.attempt_id
                or index + 1 >= len(snapshot.items)
                or snapshot.items[index + 1].item_id != successor_item_id
                or snapshot.items[index + 1].intent != QueueIntent.PENDING
                or any(i.intent == QueueIntent.NEEDS_ATTENTION for i in snapshot.items)
                or any(
                    o.state
                    in {ExecutionState.PENDING, ExecutionState.DISPATCHED, ExecutionState.UNCERTAIN}
                    for o in snapshot.commands
                )
                or any(a.attempt_id == successor_attempt_id for a in snapshot.attempts)
            ):
                raise QueueStoreError(
                    "native reservation requires the next pending item and resolved predecessor"
                )
            reservation = NativeReservation(
                reservation_id,
                source.attempt_id,
                successor_item_id,
                successor_attempt_id,
                request.scope.application_id,
                request.scope.session_id,
                request.playback_id,
                snapshot.generation,
                at,
            )
            self._native_request_matches(snapshot, reservation, request)
            if (
                source.item_id == successor_item_id
                or snapshot.items[index].content == request.successor
            ):
                raise QueueStoreError(
                    "adjacent repeated content cannot identify a native occurrence"
                )
            connection.execute(
                "INSERT INTO native_reservations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
                (
                    reservation_id,
                    queue_id,
                    source.attempt_id,
                    successor_item_id,
                    successor_attempt_id,
                    reservation.application_id,
                    reservation.session_id,
                    reservation.predecessor_playback_id,
                    snapshot.generation,
                    timestamp,
                    NativeReservationState.RESERVED.value,
                ),
            )
            self._insert_native_operation(connection, snapshot, reservation_id, request, timestamp)
            return self._changed(connection, queue_id)

    def prepare_native_clear(
        self,
        queue_id: str,
        reservation_id: str,
        *,
        request: NativeQueueRequest,
        at: datetime,
        expected_revision: int,
        expected_generation: int,
    ) -> QueueSnapshot:
        if request.action != NativeQueueAction.CLEAR:
            raise ValueError("native clear requires the clear action")
        timestamp = _timestamp(at)
        with self._transaction() as connection:
            snapshot = self._required(connection, queue_id)
            reservation = self._reservation(snapshot, reservation_id)
            self._native_request_matches(snapshot, reservation, request)
            existing = next(
                (o for o in snapshot.native_operations if o.operation_id == request.operation_id),
                None,
            )
            if existing is not None:
                if (
                    existing.reservation_id != reservation_id
                    or existing.action != request.action
                    or existing.scope_attempt_id != request.scope.request.attempt_id
                    or existing.connection_generation != request.scope.connection_generation
                    or existing.playback_id != request.playback_id
                    or type(expected_generation) is not int
                    or existing.generation != expected_generation
                    or snapshot.generation != expected_generation
                ):
                    raise RevisionConflict("native operation identity or generation differs")
                return snapshot
            self._native_fence(snapshot, expected_revision, expected_generation)
            if (
                not snapshot.cancellation_requested
                or reservation.state == NativeReservationState.SESSION_EXIT_OBSERVED
            ):
                raise QueueStoreError(
                    "native clear requires cancellation and a possibly active reservation"
                )
            if at < reservation.created_at:
                raise ValueError("journal timestamps cannot move backward")
            self._insert_native_operation(connection, snapshot, reservation_id, request, timestamp)
            return self._changed(connection, queue_id)

    def mark_native_dispatched(
        self,
        queue_id: str,
        operation_id: str,
        *,
        at: datetime,
        expected_revision: int,
        expected_generation: int,
    ) -> QueueSnapshot:
        return self._transition_native(
            queue_id,
            operation_id,
            ExecutionState.DISPATCHED,
            at,
            expected_revision,
            expected_generation,
        )

    def record_native_outcome(
        self,
        queue_id: str,
        operation_id: str,
        outcome: ExecutionState,
        *,
        at: datetime,
        expected_revision: int,
        expected_generation: int,
    ) -> QueueSnapshot:
        if not isinstance(outcome, ExecutionState) or outcome not in {
            ExecutionState.ACKNOWLEDGED,
            ExecutionState.REJECTED,
            ExecutionState.UNCERTAIN,
        }:
            raise ValueError("native outcome must be acknowledged, rejected or uncertain")
        return self._transition_native(
            queue_id, operation_id, outcome, at, expected_revision, expected_generation
        )

    def _transition_native(
        self,
        queue_id: str,
        operation_id: str,
        state: ExecutionState,
        at: datetime,
        expected_revision: int,
        expected_generation: int,
    ) -> QueueSnapshot:
        timestamp = _timestamp(at)
        with self._transaction() as connection:
            snapshot = self._required(connection, queue_id)
            self._native_fence(snapshot, expected_revision, expected_generation)
            operation = self._native_operation(snapshot, operation_id)
            reservation = self._reservation(snapshot, operation.reservation_id)
            if operation.generation != snapshot.generation:
                raise RevisionConflict("native operation belongs to an earlier owner generation")
            if at < operation.updated_at:
                raise ValueError("journal timestamps cannot move backward")
            if operation.state == state and state != ExecutionState.DISPATCHED:
                return snapshot
            if state == ExecutionState.DISPATCHED:
                if operation.state != ExecutionState.PENDING:
                    raise QueueStoreError("native operation cannot be redispatched")
                if operation.action == NativeQueueAction.PLAY_NEXT and (
                    snapshot.cancellation_requested
                    or snapshot.reconciliation_required
                    or reservation.state != NativeReservationState.RESERVED
                    or not snapshot.attempts
                    or snapshot.attempts[-1].attempt_id != reservation.predecessor_attempt_id
                    or snapshot.attempts[-1].intent != QueueIntent.CURRENT
                ):
                    raise QueueStoreError("native staging was canceled or lost its predecessor")
                if operation.action == NativeQueueAction.CLEAR and (
                    not snapshot.cancellation_requested
                    or reservation.state == NativeReservationState.SESSION_EXIT_OBSERVED
                ):
                    raise QueueStoreError("native clear requires cancellation and an active scope")
            elif operation.state not in {ExecutionState.DISPATCHED, ExecutionState.UNCERTAIN}:
                raise QueueStoreError("only dispatched native work can receive an outcome")
            connection.execute(
                "UPDATE native_operations SET state=?, updated_at=? WHERE operation_id=?",
                (state.value, timestamp, operation_id),
            )
            if state == ExecutionState.UNCERTAIN:
                connection.execute(
                    "UPDATE queue_options SET reconciliation_required=1 WHERE queue_id=?",
                    (queue_id,),
                )
            return self._changed(connection, queue_id)

    def adopt_native_successor(
        self,
        queue_id: str,
        reservation_id: str,
        *,
        observed: SessionSnapshot,
        now: float,
        at: datetime,
        decision_id: str,
        expected_revision: int,
        expected_generation: int,
    ) -> QueueSnapshot:
        _identity(decision_id)
        timestamp = _timestamp(at)
        require_instant(now, "current observation time")
        with self._transaction() as connection:
            snapshot = self._required(connection, queue_id)
            if type(expected_generation) is not int or snapshot.generation != expected_generation:
                raise RevisionConflict("native adoption owner generation changed")
            reservation = self._reservation(snapshot, reservation_id)
            scope, request, latest = observed.scope, observed.scope.request, observed.latest
            item = next(i for i in snapshot.items if i.item_id == reservation.successor_item_id)
            if (
                request.target != snapshot.target
                or request.content != item.content
                or request.queue_item_id != item.item_id
                or request.attempt_id != reservation.successor_attempt_id
                or scope.application_id != reservation.application_id
                or scope.session_id != reservation.session_id
            ):
                raise QueueStoreError("observed successor differs from its authorization")
            if reservation.adoption_decision_id is not None:
                if reservation.adoption_decision_id != decision_id:
                    raise QueueStoreError("native successor already has an adoption decision")
                return snapshot
            self._native_fence(snapshot, expected_revision, expected_generation)
            enqueue = next(
                o
                for o in snapshot.native_operations
                if o.reservation_id == reservation_id and o.action == NativeQueueAction.PLAY_NEXT
            )
            if (
                reservation.state != NativeReservationState.RESERVED
                or enqueue.state
                not in {
                    ExecutionState.DISPATCHED,
                    ExecutionState.ACKNOWLEDGED,
                    ExecutionState.UNCERTAIN,
                }
                or not observed.evidence.receiver_playback_confirmed
                or not observed.has_confirmed_playback
                or observed.ownership_lost
                or observed.stop_requested
                or latest is None
                or latest.target != snapshot.target
                or latest.content != item.content
                or latest.session_id != scope.session_id
                or latest.application_id != scope.application_id
                or latest.connection_generation != scope.connection_generation
                or latest.playback_id is None
                or latest.position is None
                or latest.connection_reset
                or latest.session_active is not None
                or latest.identity_update != IdentityUpdate.EXPLICIT
                or latest.state != PlayerState.PLAYING
                or latest.ad_active is not False
                or latest.monotonic <= scope.started_monotonic
                or not 0 <= now - latest.monotonic <= 5
            ):
                raise QueueStoreError("native adoption requires fresh qualified successor progress")
            if at < reservation.created_at:
                raise ValueError("journal timestamps cannot move backward")
            source = next(
                a for a in snapshot.attempts if a.attempt_id == reservation.predecessor_attempt_id
            )
            if snapshot.attempts[-1] != source:
                raise QueueStoreError("native predecessor is no longer the selected attempt")
            if source.intent != QueueIntent.FINISHED:
                if source.intent in {QueueIntent.CURRENT, QueueIntent.NEEDS_ATTENTION}:
                    source_decision = source.decision_id or "native-unobserved-" + decision_id
                    connection.execute(
                        "UPDATE attempts SET intent=?, decision_id=? WHERE attempt_id=?",
                        (QueueIntent.NEEDS_ATTENTION.value, source_decision, source.attempt_id),
                    )
                    connection.execute(
                        "UPDATE items SET intent=? WHERE queue_id=? AND item_id=?",
                        (QueueIntent.NEEDS_ATTENTION.value, queue_id, source.item_id),
                    )
                connection.execute(
                    "UPDATE queue_options SET reconciliation_required=1 WHERE queue_id=?",
                    (queue_id,),
                )
            connection.execute(
                "INSERT INTO attempts VALUES (?, ?, ?, ?, ?, NULL)",
                (
                    reservation.successor_attempt_id,
                    queue_id,
                    item.item_id,
                    QueueIntent.CURRENT.value,
                    timestamp,
                ),
            )
            connection.execute(
                "INSERT INTO native_attempt_origins VALUES (?, ?)",
                (reservation.successor_attempt_id, reservation_id),
            )
            connection.execute(
                "UPDATE items SET intent=? WHERE queue_id=? AND item_id=?",
                (QueueIntent.CURRENT.value, queue_id, item.item_id),
            )
            connection.execute(
                "UPDATE queues SET current_item_id=? WHERE queue_id=?", (item.item_id, queue_id)
            )
            connection.execute(
                "UPDATE native_reservations SET state=?, adoption_decision_id=? WHERE reservation_id=?",
                (NativeReservationState.ADOPTED.value, decision_id, reservation_id),
            )
            return self._changed(connection, queue_id)

    def record_native_session_exit(
        self,
        queue_id: str,
        reservation_id: str,
        *,
        at: datetime,
        decision_id: str,
        expected_revision: int,
        expected_generation: int,
    ) -> QueueSnapshot:
        """Caller has verified fresh correlated idle; this never establishes membership."""
        _timestamp(at)
        _identity(decision_id)
        with self._transaction() as connection:
            snapshot = self._required(connection, queue_id)
            reservation = self._reservation(snapshot, reservation_id)
            self._native_fence(snapshot, expected_revision, expected_generation)
            if reservation.exit_decision_id is not None:
                if reservation.exit_decision_id != decision_id:
                    raise QueueStoreError("native session already has an exit decision")
                return snapshot
            if not snapshot.cancellation_requested:
                raise QueueStoreError("native cleanup exit requires explicit cancellation")
            if at < reservation.created_at:
                raise ValueError("journal timestamps cannot move backward")
            connection.execute(
                "UPDATE native_reservations SET state=?, exit_decision_id=? WHERE reservation_id=?",
                (NativeReservationState.SESSION_EXIT_OBSERVED.value, decision_id, reservation_id),
            )
            return self._changed(connection, queue_id)
