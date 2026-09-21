# Evidence-driven queue decisions

`tellyq/domain/queue_policy.py` is the M2b queue decision component. It proposes
local changes and selects an eligible next item; it performs no I/O and is not
wired to the CLI, receiver or a running queue. Automatic handoffs still require
runner integration and supervised acceptance.

The reducer is independent of Cast, YouTube codes, storage and threading. It uses
the existing immutable `QueueSnapshot`, `SessionSnapshot` and qualified
`CompletionEvidence`. The [completion contract](YOUTUBE-COMPLETION-CONTRACT.md)
describes how the adapter and playback policy produce that witness. A reported
duration, `ENDED` state, accepted command or completion boolean without a witness
cannot finish a queue attempt.

## API

```python
decision = decide_queue(
    queue,
    session,
    now=clock.monotonic(),
    authority=authority,
    receiver=new_idle_observation,  # optional newer receiver baseline
    skip=None,  # or an explicit SkipIntent
)
```

The caller creates `QueueAuthority(queue_id, generation, connection_generation,
started_monotonic)` when acquiring ownership in the current process. Its boundary
must precede the live playback scope and subsequent evidence. It is authorization
supplied by the owner, not proof of playback, an ownership lock or a persisted
credential. Never reconstruct it from saved monotonic timestamps. The caller must
recover a reopened queue, hold exclusive ownership and feed all ordered events to
playback policy before reducing. Neither this reducer nor an acknowledged command
can establish those facts on its own.

`QueueDecision` contains the queue ID, expected revision and generation, together
with a disposition and a fixed reason:

| Disposition / proposal | Meaning |
| --- | --- |
| `HOLD`, no settlement | No next item is authorized; reason identifies uncertainty, cancellation, replacement or a correlation failure. |
| `HOLD`, `AttemptSettlement` | Propose one local attempt outcome and deterministic decision ID. Persist and reload before considering a launch. |
| `READY`, `next_item_id` | The first pending item is eligible under the supplied current evidence and authority. This is not a command receipt or a dispatched effect. |
| `COMPLETE` | The local queue has no pending items. It makes no claim about the TV's current display or whether an app was stopped. |

Settlement and next-item selection are never returned together. A durable executor
must validate expected revision/generation when committing, then check the current
owner, cancellation and receiver again at dispatch. A returned `READY` is not a
reusable permission token. Stop arriving after a decision must cancel its pending
start; a policy decision cannot close the gap between a local transaction and a
remote effect.

## Completion, cancellation and skip

A completion can settle only the queue's exact current item and attempt, matched
against the playback request's content and target. The start must be acknowledged
in the current queue generation. Pending, dispatched or uncertain command work
requires reconciliation; the reducer never resends it. Rejected starts cannot
become finished attempts merely because an unrelated receiver snapshot exists.

The deterministic settlement ID includes the queue, item, attempt, connection,
session and exact terminal witness. Repeated reduction before persistence proposes
the same ID. After settlement, it does not propose another settlement. A finished
attempt must still match its original live witness before it can authorize a next
item; durable `finished` state alone cannot restore permission after restart.

Queue cancellation, playback stop intent and any journaled operator stop dominate
completion and skip decisions. `SkipIntent` requires a caller-generated intent ID
and exact attempt/item IDs. It proposes `SKIPPED`, never `FINISHED`, and does not
claim that playback stopped. An active skipped program still blocks the successor
until the owner supplies an appropriate new receiver baseline. This is a local
intent contract; no skip command has been added to the CLI.

## Historical completion is separate from launch permission

The original completion remains useful when a subsequent observation is an ad,
unknown state, buffering, disconnect or replacement. The reducer may propose its
historical settlement, even if the terminal is now old, while holding advancement.
Known replacement remains a hold even when a later idle packet is supplied;
explicit ownership reconciliation belongs to the future runner.

A pending successor needs either the still-fresh, exact qualified terminal that
produced the witness, or a newer explicit receiver-idle baseline. The baseline
must match target and connection, follow the owner's acquisition boundary and be
within the caller's freshness policy. It must also follow every relevant session
observation in both sequence and monotonic time; an old idle message cannot erase
newer playback. Newer receiver heartbeats do not refresh the historical terminal.
New ad, unknown, paused, playing or buffering observations cannot reuse it as
launch permission. If the owner cannot establish a safe baseline, the queue holds.

This matters for the live M2a trace: a verified ending was followed by unknown
code-5 buffering and, in one run, ad/replacement content. This component deliberately
does not convert those later states into permission to overwrite the receiver.

The existing `QueueStore.prepare_stop` is an operator-stop operation: it requires
an unresolved current attempt, cancels the queue, and its journal entry blocks a
natural-finish settlement. It cannot double as between-item cleanup after
settlement. M2b integration therefore still needs an explicit handoff or owned
release/reconciliation contract. Weakening completion or treating unknown receiver
state as idle is not a substitute for that contract.

## Verification boundary

Synthetic ordered playback traces cover exact and anonymous completion, duplicate
terminals, terminal followed by replacement/ad/unknown updates, cancellation
between settlement and start, explicit skip, stale/contradictory idle snapshots,
restart boundaries, uncertain commands and scope mismatches. An isolated import
check proves the module loads without optional dependencies or runtime I/O.
The durable-store bridge applies the proposal to SQLite, reloads it, verifies
duplicate settlement idempotence and demonstrates cancellation between settlement
and next-item preparation. It performs no device dispatch.
These tests execute no receiver commands and establish no new live handoff result.
