# Native queue durability foundation

`SQLiteQueueStore` and `NativeQueueExecutor` provide the durable boundary for the
opt-in native queue plan in [M2-NATIVE-QUEUE-PLAN.md](M2-NATIVE-QUEUE-PLAN.md).
This foundation does not select a live receiver, enable native mode in the
foreground service, or close the M2 live acceptance gate.

## Durable intent and migration

Schema version 2 adds queue options, native reservations, native operations and
native attempt provenance. Opening a valid version 1 database migrates it in one
transaction, preserves the original five tables and their rows, and marks every
existing queue `legacy`. Unsupported schemas or invalid version 1 values are
retained without migration. Native mode must be requested when creating a queue;
there is no in-place mode toggle for an existing queue.

An initial native queue item uses the existing START journal. Its next item may
have one outstanding `reserved` reservation. The reservation binds the selected
predecessor attempt, successor item and future attempt, application/session, and
original owner generation. The successor remains pending until observation.
Adjacent equal content is refused because video identity alone cannot distinguish
those occurrences. Nonadjacent repeats such as A → B → A use separate item and
attempt identities. One local reservation makes no claim about receiver playlist
cardinality, autoplay, or work staged by another controller.

Every reservation has one PLAY_NEXT operation, with at most one CLEAR operation.
Each records `pending`, `dispatched`, `acknowledged`, `rejected` or `uncertain`.
Preparation and the exclusive dispatch claim commit before receiver I/O. A
duplicate operation only returns history, including a pending intent left by a
crash. A rejected operation also cannot be automatically retried with a new ID.
No automatic local START or release fallback is permitted for a staged successor.

## Effects and ownership

`NativeQueueExecutor.execute(NativeExecutionRequest(...), operation,
may_effect=...)` prepares and claims the operation, rereads the durable revision
and generation, applies the caller's final guard, then invokes its bounded effect
once. The caller must hold exclusive foreground ownership throughout that gap
and verify fresh receiver scope inside its backend. A claim is not a transferable
effect authorization. A complete matching native receipt is required; command
return never proves queue membership, playback, completion, or cancellation.

Ordinary operation errors and unknown outcomes become uncertain and set a
reconciliation hold. Process interruption may leave a dispatched operation. If
recording an outcome fails, `NativeOutcomePersistenceError.result` preserves the
last known dispatch and observed receipt; reload durable state and reconcile
without resending. A write may have committed before its response was lost.

## Passive adoption and recovery

`adopt_native_successor` consumes a caller-qualified `SessionSnapshot` produced
from fresh observations. It requires current non-ad progress, exact successor
content, item/attempt and target, the original application/session, a current
connection scope, and a fresh explicit media observation. PLAY_NEXT must already
be dispatched, acknowledged or uncertain. Pending or rejected intent cannot
authorize adoption. Persisted wall timestamps and old monotonic clocks never
establish fresh observation authority.

Adoption atomically selects a new `native_observed` attempt with no synthetic
START command or receipt. The caller first records A's qualified completion when
available. Otherwise adoption retains existing settled facts or marks unresolved
A `needs_attention`, selects B, and holds further staging. Expected B is never
evidence that A finished. Cancellation and existing reconciliation holds survive
adoption. Ads, unknown telemetry and takeover do not satisfy adoption.

Explicit owner recovery increments the generation for a native queue with any
attempt or reservation, marks dispatched effects uncertain and sets the hold.
It leaves the selected CURRENT intent intact so fresh reconnection evidence can
authorize control of an already-running approved A or B. CURRENT is a local
selection, not restored playback proof. Recovery must occur once when acquiring
exclusive ownership, after the prior owner can no longer dispatch; repeated
explicit calls each fence the previous generation. Reading status alone does not
dispatch, clear holds, replay pending commands, or launch anything.

The user-approved successor may continue while the controller is disconnected.
A local hold cannot retract receiver work. Fresh observation can adopt that
approved successor after recovery, but neither reconnection nor adoption clears
the hold automatically.

## Cancellation

Local cancellation is distinct from reconciliation. CLEAR requires durable
cancellation first and an owned A or B scope in the reservation's original
application/session. Unknown CLEAR retains uncertainty and must not prevent a
separate, freshly guarded owned STOP. An accepted CLEAR alone does not prove an
empty remote playlist or cessation of playback.

`record_native_session_exit` records the caller's decision only after fresh,
correlated idle evidence following the owned stop/release boundary. It does not
infer membership from command return. The caller records the exit for each
affected reservation. Session exit does not clear cancellation or reconciliation.

Offline tests cover migration, one-reservation contention, passive adoption,
separate predecessor completion, recovery generation fences, cancellation,
receipt correlation, and crashes before/after preparation, claim, receiver call
and outcome recording. Hardware acceptance remains a separate requested run.
