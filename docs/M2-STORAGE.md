# M2 durable queue foundation

`SQLiteQueueStore` is an offline foundation for the future single-owner runner.
The current CLI still uses the existing one-item JSON queue and JSON session store.
Creating or importing this database does not change CLI behavior, control a TV,
monitor playback, select the next item or enable automatic advancement. M2a's
real lifecycle evidence remains a prerequisite for that behavior.

## Contract and data

`domain/queue.py` supplies immutable values and the `QueueStore` protocol. A queue
has one target, ordered content references, an explicitly selected item and a
revision. Content IDs and providers are opaque; new queues are not restricted to
Bob Ross or YouTube. Separate records hold queue intent, attempts and command
execution. There are no persisted observations, connection sessions, monotonic
clocks or receiver-playback claims.

Queue intent distinguishes `pending`, `current`, `finished`, `skipped`, `stopped`
and `needs_attention`. A command moves from `pending` to `dispatched`, then to
`acknowledged`, `rejected` or `uncertain`. Acknowledgement is only command history;
it does not mark an item playing or finished. An uncertain command cannot be
redispatched. A future owner may record an observed outcome after reconciling it.

- `create` and `load` produce detached snapshots. Creating a queue requires pending
  items; loading makes no recovery decisions.
- `prepare_start` atomically selects an explicit pending item and stores its
  attempt and pending command. It does not choose which item should run next.
- `mark_dispatched` commits before the owner calls the device. Only a current,
  unresolved attempt with an eligible command can dispatch. Repeating dispatch is
  rejected even if the caller has reloaded the newest revision.
- `record_outcome` stores a command result independently of playback. Network I/O
  happens after the database transaction has ended.
- `prepare_stop` atomically cancels further starts and records a pending stop
  command for the current attempt. `cancel_queue` cancels local starts even when
  no attempt exists or between items; it makes no claim that the TV stopped.
- `settle_attempt` records an explicit caller decision with a stable decision ID.
  The caller must supply the relevant fresh evidence or operator decision; storage
  does not run completion policy. A recorded stop intent cannot become natural
  completion. Settlement retains the selected item and never advances the queue.

All mutations compare the queue revision. Repeating identical preparation with
the same command ID, or settlement with the same decision ID and outcome, returns
existing history, including after a lost local response. Reusing an identity for
another intent fails. A returned historical command is not permission to resend
it. Command/attempt/decision IDs are globally unique within the database; queue
item IDs are unique within a queue.

## Minimal Python example

This example only writes private local intent. It sends no playback commands.
Use a separate runtime directory when exploring it alongside the legacy CLI.

```python
from datetime import UTC, datetime
from pathlib import Path

from tellyq.domain.queue import QueueEntry
from tellyq.domain.values import ContentRef, PlaybackTarget
from tellyq.queue_store import SQLiteQueueStore

store = SQLiteQueueStore(Path("runtime/queue-experiment"))
queue = store.create(
    "evening-1",
    PlaybackTarget("synthetic-device", "cast"),
    (
        QueueEntry("first", ContentRef("youtube", "opaque-first")),
        QueueEntry("second", ContentRef("youtube", "opaque-second")),
    ),
)
prepared = store.prepare_start(
    queue.queue_id,
    "first",
    attempt_id="attempt-1",
    command_id="start-1",
    at=datetime.now(UTC),
    expected_revision=queue.revision,
)
assert prepared.commands[0].state == "pending"
```

A future runner must hold exclusive ownership before dispatching or recovering.
This store provides transaction serialization, not the runner's lifetime lease.
The example intentionally stops at durable intent.

## Restart and cancellation boundaries

Opening the database is safe for a status reader and does not interfere with an
active owner. Only an exclusive runner owner may explicitly call `recover`.
Recovery invalidates old command generations, cancels further starts and marks
current attempts/items `needs_attention`. A pre-dispatch command remains pending;
a dispatched command becomes uncertain. Acknowledged commands retain their
historical result without establishing current playback or session ownership.
Completed decisions remain durable and duplicate terminal events cannot apply
twice. Recovery does not change a terminal item into a finished item or resend
anything. There is no claim of exactly-once delivery to the remote device.

After recovery, a future reconciliation API must obtain fresh device evidence
before clearing attention or cancellation. This foundation deliberately has no
API to clear those barriers or restore ownership. It is not yet a usable restart
runner. Generation and revision checks reject outcomes/dispatch from stale
workers, but SQLite alone cannot retract a remote operation already in flight.
The future owner must serialize effects and cancellation around that boundary.

## Explicit legacy import

```python
imported = store.import_legacy(Path("runtime"), queue_id="legacy-1")
```

Import uses the existing strict version-1 JSON validator, including its single
Bob Ross constraint. The source JSON remains intact; repeated import with the
same queue identity and validated source is idempotent. A changed source is
rejected, preserving both histories. The new database format supports arbitrary
content; the old JSON schema has not silently changed.

Legacy `queued` becomes pending; `stopped` and `finished` remain historical intent.
`starting`, `playing`, `unconfirmed` and `failed` require attention and cancel
starts. Import creates no attempt, command, live evidence or session ownership.
It does not migrate legacy `session.json` or the JSON session-snapshot files.
An imported historical `finished` value is not new natural-completion proof.

## Storage and validation

The runtime directory is mode `0700`; the database and SQLite rollback journals
are mode `0600`. Each operation owns and closes its own SQLite connection.
Explicit transactions commit queue position, attempts and intents together, with
foreign keys, full synchronization, revision checks and rollback on failure.
No connection or database lock is held across network work. Symlink runtime/DB
paths are rejected. Unknown schema versions, schema changes (including added
triggers, views or custom indexes) and corrupt files
fail with the original data retained; opening never overwrites them with an
empty queue. Schema validation runs again before each transaction, so a schema
change after opening cannot alter cancellation or other journal updates. SQLite's
automatic indexes for the declared primary-key and uniqueness constraints remain
supported.

Focused offline tests cover independent thread/process writers, rollback after
an intent collision, cancellation/start races, stale dispatch, uncertain effects,
reopening at dispatch boundaries, duplicate decisions, schema rejection, private
permissions and strict legacy import. They establish local durability semantics,
not hardware completion, remote deduplication or the M2 stop-latency targets.
