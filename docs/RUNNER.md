# Persistent owner and mailbox foundation

`SessionRunner` supplies a managed worker, private ownership locks, a bounded
mailbox and a cached immutable read model. It does not yet compose a Cast backend,
SQLite queue, queue policy, IPC endpoint or `tellyq serve` command. Existing CLI
behavior is unchanged. Importing or constructing a runner creates no files,
threads or device connection; `start()` is the explicit activation boundary.

## Small task contract

The owner creates its task by calling `task_factory(cancellation)` on one
**non-daemon** worker thread after acquiring ownership. That same thread calls
`task.step(cancellation)`, `task.handle(command, cancellation)` and `task.close()`.
Device clients, connection creation and cleanup belong there. A factory must
clean up its own partial construction if it raises. Factory construction must
not start media; explicit commands and the later queue policy authorize effects.

Each method must bound its own I/O. Python cannot safely terminate arbitrary
blocked task code; the owner does not claim it can enforce a remote deadline.
The step interval is between zero and two seconds, exclusive of zero, and controls
when another observation step is due. A due step runs before regular queued
commands so command traffic cannot indefinitely starve observations. An accepted
stop takes priority over both. Neither observation nor command I/O holds the
mailbox/snapshot lock.

`RunnerCommand` uses the existing `CommandAction`: start, pause, resume or stop.
Start carries an explicit `PlaybackRequest` for the runner's target. The task is
responsible for ownership, capabilities, command journaling and evidence checks;
merely routing pause/resume through this mailbox establishes no support.
`TaskView` contains the existing frozen playback and queue snapshots. The runner
publishes it inside a frozen `RunnerSnapshot` with its phase, revision, ownership,
cancellation flag, pending count and current/last command metadata. `snapshot()`
only reads this cache; it does not contact the device, wait for the worker, or
refresh playback evidence. The task must apply freshness policy to its view.

## Commands and cancellation

`submit(command)` returns a caller-owned `CommandTicket` immediately or raises
`MailboxFull` without queuing anything. The default regular mailbox holds sixteen
commands; the configured positive capacity is explicit. The owner retains only
that mailbox, one active command, one reserved stop ticket and its latest read
model. It does not accumulate completed tickets or a command history.

Tickets distinguish queued, running, handled, canceled and failed. `handled`
means the task returned normally; it is not a device acknowledgement or playback
proof. Inspect the task view's command receipt and evidence. A ticket wait timeout
does not withdraw the command or justify resending it.

`stop(command_id)` immediately latches cancellation, cancels pending starts and
resumes, and reserves one priority stop outside the regular mailbox capacity.
Repeated stop requests coalesce to that original ticket, even with different
IDs or after failure; no list of stop waiters grows inside the runner. A known
command ID cannot describe different work across the regular, active and reserved
stop slots. Duplicate identical pending/active regular commands share their ticket.
Completed regular commands are **not globally deduplicated** by this mailbox;
the durable executor and journal supply that guarantee in the integration wave.

`cancel()` only sets the same local latch and cancels pending starts/resumes. It
sends no remote stop. Cancellation is permanent for this runner instance; it
cannot be cleared by status, another command or restarting the same object. A new
session requires a deliberate new runner and the future reconciliation policy.
Pause and explicit stop remain routable after cancellation.

A command selected before cancellation is already in flight, even if its task is
still preparing the effect. The factory receives the same latch before it returns,
and **the task must check it at the final effect boundary**, including after any
blocking preparation. Already-dispatched network operations cannot be retracted.
A typical task-side guard looks like this:

```python
if command.action in {CommandAction.START, CommandAction.RESUME}:
    if cancellation.requested:
        return current_view
# Persist/check intent, then recheck cancellation immediately before device I/O.
```

The next wave's durable executor accepts this final cancellation callback. The
owner alone cannot turn a non-cooperative task into safe cancelable code.

## Ownership and shutdown

The runtime and owner-lock directories are mode `0700`; lock files are `0600`.
The receiver lock filename hashes the stable device ID, not an address or friendly
name. Lock acquisition is nonblocking and excludes another process using the same
runtime directory. Lock files are retained after release; file existence is not
proof of a live owner. The operating system releases the lock if the process dies.
Do not delete/recreate lock files while a process holds them.

For compatibility, the runner also holds the existing runtime `command.lock`
inode using the same `flock` protocol as `state.command_lock`. This deliberately
blocks **all legacy CLI commands and other runners in that runtime**, including
legacy status and commands for another receiver, for the owner's lifetime. Cached
runner status remains available through the library. The later IPC integration
must route clients to this owner rather than invoke the legacy controller inside
it. Separate runtime directories do not share these locks and must not target the
same device concurrently. This is local process ownership, not a receiver-side
lease against Google Home or another Cast sender.

`shutdown(timeout)` permanently cancels pending regular commands, finishes any
already accepted reserved stop, and asks the worker to close its task. Shutdown
itself does not send a TV stop. Creation, command work and cleanup remain on the
owner thread. A timeout raises `RunnerTimeout` while the worker continues to own
its task and locks; the caller never closes resources or releases ownership.
Calling shutdown again can wait for completion. A blocked close retains ownership
as well. A non-daemon worker keeps the process alive, so the foreground host must
resolve a stuck task or explicitly terminate the process; abandoning a handle is
not successful shutdown.

Task failures cancel pending work and trigger owner-thread cleanup. Cached failures
use stable categories for ownership, initialization, command, observation and
cleanup; arbitrary backend exception strings are not exposed. A cleanup failure
reports failed rather than claiming a successful shutdown. A stopped worker has
released its locks. None of these phases means the TV stopped playing.

## Checks and remaining integration

Offline tests use events and barriers rather than sleep-based ordering. They cover
same-thread lifecycle, responsive cached status during blocked work, stop priority,
capacity/concurrent producers, identity collisions, cancellation at the final effect
boundary, initialization/cleanup timeouts, safe error paths, immutable historical
snapshots and subprocess exclusion of both receiver and legacy locks. Tests block
sockets and never control hardware. Standard Python 3.14 is the supported runtime;
this work makes no free-threaded compatibility or measured LAN-latency claim.

The next integration wave supplies the foreground CLI, private IPC, queue/backend
composition, durable dispatch, recovery and evidence-driven advancement. This
foundation does not complete the three-run/six-handoff M2 acceptance gate.
