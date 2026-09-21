# Persistent playback task and owned release

`PlaybackTask` composes `PlaybackApplication`, the pure queue reducer,
`QueueExecutor` and a supplied `QueueStore` behind the existing `RunnerTask`
protocol. Construct it inside `SessionRunner`'s owner factory, with an injected
backend context factory, session store, clock, queue ID and exact target. Resource
construction, operations and close stay on that thread. Close disconnects resources
without inventing an operator stop. The backend contract must bound every call.

Construction never starts content. A new queue awaits an explicit START request
for its first pending exact item and a fresh positive receiver-idle baseline.
Existing attempt history is recovered into a durable hold; no process-local
ownership or monotonic evidence is loaded to resume playback automatically.
Status snapshots reuse the immutable `TaskView`. Holding advancement still
consumes observations, including delayed release verification.

## Durable sequence

Each remote action has committed intent, a single dispatch claim and a separately
recorded receipt. START, PAUSE, RESUME and STOP use the application ownership and
capability guards. A delegating backend additionally checks queue revision,
generation, cancellation and applicable receiver evidence immediately before the
effect, after any blocking application baseline. STOP remains allowed when the
operator cancellation latch is set. Duplicate command IDs cannot describe another
attempt, item or content. Uncertain effects and failed outcome persistence never
cause automatic resend.

A qualified ending is settled as FINISHED first. In a later step, an internal
RELEASE may close the still-owned completed receiver session. This distinct journal
action never cancels the queue or rewrites the completion decision; it is not a
public runner command. The store permits only one release of the latest FINISHED
attempt and fences pending/dispatched/uncertain releases on recovery. The schema's
existing action TEXT field accepts the additive action without a schema change;
older binaries do not understand the new value and must not operate that journal.

An accepted RELEASE receipt does not establish an idle receiver. The application
records a process-local release boundary and requires explicit receiver idle after
that boundary, in the same connection, with no intervening contradiction. Only
then is `release_confirmed` true. Completion remains historical and
`stop_requested` stays false. Those release proof fields are never restored by
the existing session JSON decoder. A subsequent fresh idle observation and the
queue policy's next-item decision are required before preparing the successor.
Cancellation between settlement, release and start prevents advancement.

## Release authority and remaining live limitation

The process preserves the actual terminal observation as a separate release
anchor. Release requires fresh exact current content, app, session, media and
connection identity, a matching receiver, and no positive ad evidence. The terminal
must still be within five seconds. Sequence gaps, resets, replacement, changed
media ID, post-terminal PLAYING/PAUSED or backward position permanently invalidate
this automatic release authority. Current omitted identity holds. The final guard
rechecks all evidence after the application's blocking observation.

UNKNOWN provider/ad metadata can remain unknown while this narrower contract
authorizes closing the already-completed owned session: current media must still
be IDLE/FINISHED or BUFFERING with exact unchanged identity and no contradiction.
This does not interpret an unknown provider code, qualify another ending, mark ads
inactive or authorize a successor. Only the later positive idle can do that.

The actual M2a trace transitions from anonymous FINISHED at duration to explicit
same-title BUFFERING with YouTube code 5 and **position 0.0**. That backward-position
transition remains a HOLD regression. The inspected YouTube implementation does
not supply a defensible meaning for code 5 or this position reset. Successful fake
handoffs therefore do not establish a live automatic handoff for that trace.
No timer or fabricated inactive-ad field bypasses the hold.

Operator STOP may act on a still-owned finished session while preserving its
already-durable FINISHED outcome and canceling the queue. It is deliberately
different from internal release. Known takeover remains protected from both paths.

## Offline evidence

Composition tests run an actual three-item reducer/application/SQLite queue
against a synthetic bounded backend, with separately observed completion, release
and idle transitions. They cover pause/resume, explicit stop, duplicate commands,
cancel during blocking baselines, same-batch terminal/replacement, post-release
interference followed by idle, delayed idle, uncertain/rejected release and
pre/post-dispatch recovery. Acknowledged historical release never restarts a
successor after process recovery. Normal tests perform no hardware or network I/O.
