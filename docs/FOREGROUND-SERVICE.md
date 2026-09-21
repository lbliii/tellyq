# Foreground queue service

The M2b composition connects `PlaybackTask`, `SessionRunner`, SQLite command
journaling and private Unix-socket IPC. One ordinary Python 3.14 owner thread
creates, uses and closes the Cast backend. Client processes submit work and exit;
the foreground process continues observing. Imports, help, manifest validation
and service construction do not start playback. `serve` connects the backend;
an explicit `start` begins the first item after fresh idle evidence.

This implementation has offline acceptance. **The M2b live handoff gate is still
pending.** A confirmed natural ending followed by YouTube code 5, BUFFERING and
position zero is deliberately held. This exact pattern occurred in two M2a runs;
the pinned first-party source does not establish whether it is safe cleanup or
new activity. It is not inactive-ad evidence or permission to launch another item.

## Running a session

Copy [the example manifest](../examples/session.json) to an ignored runtime file.
Replace the placeholder device UUID and queue ID. Keep real device identifiers in
`runtime/`. Each manifest has one exact Cast target, 1–32 exact YouTube video IDs
and unique item IDs. Repeated titles use different item IDs. A durable queue ID
cannot be reused for different content, order or target.

From the repository root, keep this process running:

```sh
uv run --locked tellyq serve --manifest runtime/session.json
```

After its `ready` JSON event, use another terminal in the same directory:

```sh
uv run --locked tellyq start
uv run --locked tellyq status
uv run --locked tellyq pause --command-id pause-1
uv run --locked tellyq ticket --command-id pause-1
uv run --locked tellyq resume --command-id resume-1
uv run --locked tellyq stop --command-id stop-1
uv run --locked tellyq shutdown --timeout 5
```

Use `tellyq --runtime PATH ...` on every command to isolate another local runtime.
Owner exclusion is scoped to that runtime; use one shared runtime for a receiver.
Separate runtime directories do not coordinate ownership of the same physical TV.
`start` always means the manifest's first item. Its default command and attempt
IDs are stable for the queue, so retrying cannot select a later item. The owner
alone chooses subsequent items. An explicit `--command-id` supports correlating
other operations and querying their tickets. If stop requests coalesce, query the
canonical `ticket_id` returned in the response.

`accepted` means the local mailbox accepted the command. A ticket marked `handled`
means the task returned. Neither proves TV playback or even a successful remote
command. Inspect the ticket's view and fresh `status`: the receipt outcome,
receiver evidence, observation time, ownership and historical completion are
separate fields. A hold can retain a historical finish while current ownership or
handoff evidence is unavailable. Status is cached and does not wait for device I/O.

Stop cancels advancement immediately and reserves a priority owner command.
Shutdown and Ctrl-C cancel local work and close resources; they do not implicitly
stop the TV. Send stop first when that is intended. A blocked operation keeps its
owner locks and IPC endpoint until its worker actually exits. Initialization may
report `initializing` before the endpoint becomes ready.

When an owner endpoint exists, the legacy control command names use IPC. A stale,
invalid or unavailable endpoint produces an error; it never falls back to a second
Cast controller. Without an endpoint the original one-item commands still work.
`--seconds` remains a direct-command observation option and is rejected in owner
mode; use status/ticket queries there. `--command-id` requires owner mode.

## Durable state and recovery

The service uses `queue.sqlite3`, private session snapshots and `runner.sock`
under the selected runtime. IPC is local to the account, bounded in frame size,
client concurrency and command history; it does not expose a TCP listener. See
[LOCAL-IPC.md](LOCAL-IPC.md) for wire contracts and capacity errors.

The foreground Cast adapter retains only its latest raw observation window.
Private takeover guards retain at most six identity witnesses, preserving the
latest mismatch for every possible scope boundary even after identity returns to
its original value. These witnesses do not replace ordered lifecycle evidence.
Short diagnostic commands retain their existing raw-capture behavior.

Legacy JSON import is explicit:

```sh
uv run --locked tellyq serve --import-legacy --queue-id imported-session-1
```

This validates and imports the current runtime's legacy one-item queue without
rewriting the source queue or session. Unresolved legacy state requires attention.
A restarted queue with attempt history cannot regain process-local ownership or
silently repeat an uncertain command. Inspect its durable state, resolve receiver
activity deliberately, and use a new queue ID for a new session. There is no
automatic retry, replay, skip or recovery-override command.

Natural completion settles an attempt first. Internal RELEASE is separately
journaled, guarded cleanup of that finished attempt; it is not exposed as a client
command and does not cancel the queue. A successor additionally requires observed
idle after cleanup, retained ownership authority and no cancellation, replacement,
ad, reset or replay veto. See [PLAYBACK-TASK.md](PLAYBACK-TASK.md).

## Validation boundary

Normal tests block sockets and DNS. Hermetic subprocess tests allow only AF_UNIX
while rejecting IPv4, IPv6 and DNS; they exercise actual local transport with
synthetic tasks/backends. The composition scenario runs CLI → IPC → owner →
PlaybackTask → SQLite for three items, retries start after advancement, verifies
three starts and three releases, and closes the endpoint with one backend owner.
These are simulated receiver observations, not new hardware evidence.

The next supervised hardware checkpoint first checks ready/status, explicit
start, local stop acceptance, observed stop and shutdown on the composed service.
Only a supported, evidenced handoff proceeds toward the roadmap's three
three-item sessions and six handoffs. An unexplained code-5/reset sequence is a
recorded hold, not a passed handoff. No M2b hardware run occurred while building
this composition.
