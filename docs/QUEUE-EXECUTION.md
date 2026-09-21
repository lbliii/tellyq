# Durable command execution

`QueueExecutor` adds a dispatch boundary around the existing `QueueStore`. It is
an independently tested component for the M2b owner. It does not wire SQLite into
the CLI, run a receiver, choose the next item or enable automatic advancement.
The current CLI and its legacy queue/session files retain their existing behavior.

## Request, effect and history

A frozen `QueueCommandRequest` identifies the queue, item, attempt, action and
stable command ID, plus the expected queue revision and runner generation. Start
and stop are supported. The caller chooses the item and supplies a bounded
operation receiving a `QueueDispatch` and returning a `CommandReceipt`. The
operation must still enforce fresh receiver ownership and capability guards;
durable queue identity is not proof of current TV ownership.

The executor performs these steps:

1. Validate the generation and inspect existing command history. An existing ID
   can only describe the same item, attempt and action. An identical existing
   command returns history without invoking the device, even if it is still
   pending after an interrupted preparation.
2. Atomically prepare intent using the expected revision. Start selects the
   explicit item; stop also commits queue cancellation. Queue cancellation is
   local intent, not a claim that playback stopped.
3. Claim the pending command through `mark_dispatched`. This is a strict atomic
   transition, not an idempotent acknowledgement: two competing callers cannot
   both win the same dispatch. The transaction commits before any effect.
4. Reread the revision/generation and check committed cancellation. Immediately
   before a start, optionally consult `may_start`, the owner's process-local
   cancellation guard. A false result or guard error holds the command as
   uncertain without invoking the operation. This start guard does not prevent
   an explicit stop from running.
5. Invoke once and record the receipt independently. The receipt's request ID
   must equal the command ID, and its attempt/action must match. Accepted maps to
   acknowledged, rejected to rejected, and unknown to uncertain. An invalid
   receipt or an ordinary operation exception becomes uncertain.

`ExecutionResult` distinguishes whether this invocation entered the operation,
the durable command state, a newly returned receipt, and any fixed problem label.
Pending, dispatched and uncertain historical commands explicitly report
`reconciliation_required`. No historical receipt is fabricated from an
acknowledgement. No receipt marks an item playing, finished or stopped; attempts
remain unresolved until a separate evidence or operator decision settles them.
There are no persisted process-local clocks or playback observations here.

STOP is an operator stop: it cancels queue advancement and prevents that attempt
from later being settled as natural completion. It must not be reused as
between-item cleanup. A dedicated handoff/reconciliation operation belongs to
later runner integration, after a completion decision has been preserved; this
component does not weaken stop semantics to implement that behavior.

## Cancellation and ownership limits

The caller must hold one exclusive playback-owner lease and serialize remote
effects. A new owner may recover only after the previous owner can no longer
invoke an effect. SQLite's revision/generation checks and the final reread fence
stale local decisions, but cannot make a committed journal update and a Chromecast
call one atomic operation. Cancellation or takeover after the last check can
still race a call already entering the device. An already dispatched effect may
have happened; it must not be silently replayed.

The optional start guard narrows this final gap and connects the executor to a
responsive cancellation latch without holding a database transaction over I/O.
It must be fast and side-effect-free. Operations must be bounded by their adapter;
the executor does not launch helper threads, kill running calls or implement an
additional timeout that could disguise an ongoing remote effect.

If cancellation or recovery changes durable state between dispatch and the
final reread, invocation is refused. The existing dispatched record remains a
conservative unresolved history for recovery. If the queue changes during the
effect, the stale outcome write fails rather than overwriting the newer state.

## Failure and restart boundaries

Before dispatch, storage errors propagate and the operation is not called.
Process interruption can leave either pending intent or dispatched work. An
ordinary operation exception records uncertainty because delivery is unknown;
a process-ending exception can leave dispatched state for explicit recovery.

If outcome persistence fails, `OutcomePersistenceError.result` retains the last
known dispatch snapshot, whether the operation ran, the returned matching receipt
if any, and the problem label. The failing write itself might have committed
before losing its response. Reload the database and hold for reconciliation;
do not resend the device command or assume the exception's snapshot is current.
No automatic write retry or effect retry occurs.

`recover(queue_id, expected_revision=..., expected_generation=...)` is explicit
and requires the exclusive new owner. It uses the existing store recovery:
current attempts need attention, queue starts are canceled, old workers are
fenced, and dispatched work becomes uncertain. Pending intent is never replayed;
accepted/rejected receipts and completed decisions remain historical. Recovery
does not restore receiver ownership or monotonic evidence, clear attention, or
start a successor. A stale command stays fenced even if its caller reloads the
new generation number.

Legacy import remains a separate explicit `SQLiteQueueStore.import_legacy`
operation. The executor never imports, rewrites or removes legacy JSON files.

## Offline verification

Tests use real SQLite files reopened at pre-prepare, post-prepare, pre-dispatch,
post-dispatch, post-effect, pre-outcome and post-outcome failure boundaries.
Coverage also includes simultaneous same-ID requests, in-flight duplicate reads,
receipt mismatches, start/stop cancellation semantics, final guard refusal,
late receipts after recovery, preserved completion decisions and independent
writers during an effect. These tests establish local persistence and refusal
behavior, not exactly-once remote delivery, live queue handoffs or stop latency.
