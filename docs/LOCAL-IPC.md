# Private local owner protocol

`RunnerIPCServer` exposes a `SessionRunner` that has already started and acquired
its receiver and legacy runtime locks. It contains no Cast or queue implementation.
Construction and import create no sockets or threads. The caller must supply the
same runtime directory as the runner. CLI/service composition owns the foreground
lifetime; this library does not create a daemon or release the runner's locks.

```python
from tellyq.runner_ipc import RunnerIPCClient, RunnerIPCServer, endpoint_path

runner.start()
server = RunnerIPCServer(runtime, runner)
server.start()

client = RunnerIPCClient(runtime)
status = client.status()
accepted = client.submit(command)
result = client.ticket(accepted["ticket_id"])
shutdown = client.shutdown(timeout=0)
# A timeout retains ownership and the endpoint. Close only after the worker exits.
if not runner.snapshot().owns_device:
    server.close()
```

`endpoint_path(runtime)` is `runtime / "runner.sock"`. Once this endpoint exists,
clients must route commands to the owner. Connection/protocol failure is an
unknown local outcome and must never trigger fallback to a direct device command.
A malformed request that fails client-side encoding raises `IPCProtocolError`;
transport or response-validation failure raises `IPCUnavailable` with a stable,
non-payload-bearing message.

## Version 1 contract

One UTF-8 JSON object, followed by a newline, per connection. Requests and responses
are bounded to 65,536 bytes including the newline. Duplicate fields, unknown fields,
non-finite numbers, unsupported versions and invalid field types are rejected.
`runner_codec` validates untrusted requests into immutable domain commands; response
decoding validates the envelope, ticket/snapshot fields and bounded JSON projections.
`models.py` owns the shared wire TypedDicts. Projections are read-only JSON, never
untrusted domain-object hydration.

| Operation | Required request fields in addition to `schema_version: 1` |
| --- | --- |
| `status` | `operation: "status"` |
| `submit` | `operation: "submit"`, `command: {command_id, action, request}` |
| `ticket` | `operation: "ticket"`, `command_id` |
| `shutdown` | `operation: "shutdown"`, `timeout` in seconds, 0 through 5 |

Only `start`, `stop`, `pause` and `resume` are external actions. The whitelist is
explicit: adding an internal enum member, such as receiver release, does not expose
it to IPC. Start requires the complete playback request:
`{request_id, attempt_id, queue_item_id, content: {provider, content_id, kind, title},
target: {device_id, route, name}}`. Kind is `video` or `episode`; title/name may be
null. Every other action requires `request: null`. Strings are nonempty and at
most 2,048 characters. Pause/resume only route through the supplied task; IPC does
not assert receiver capability or observed effect.

Every response has `{schema_version: 1, ok, code}`:

- `status`, `shutdown`, `shutdown_timeout` include `snapshot`. Shutdown timeout has
  `ok: false`; the snapshot honestly retains `owns_device` until the worker exits.
- `accepted` and `ticket` include `ticket_id` and `ticket`. Acceptance means local
  submission. Ticket states are `queued`, `running`, `handled`, `canceled`, `failed`.
  **Handled means the task returned, not that the receiver accepted or performed it.**
- Other codes have `ok: false`: `invalid_request`, `command_conflict`,
  `command_rejected`, `runner_unavailable`, `mailbox_full`, `history_full`,
  `ticket_missing`, `server_busy`, `response_too_large`, `internal_error`.

The snapshot includes `phase`, `revision`, `owns_device`,
`cancellation_requested`, `pending_commands`, `active_command_id`, `last_command_id`,
`failure`, and `view`. Ticket fields are `command_id`, `action`, `state`, `failure`,
and nullable `view`. Failure values use the runner's stable typed reasons; arbitrary
exception messages, recovery prose and private backend payloads are not serialized.

`view` has nullable `playback` and `queue` projections. Playback retains the actual
observation, receipt and completion timestamps; polling does not refresh old
observations or make historical completion current evidence. Unknown ad state
stays null. Command receipt outcome, observed playback evidence and historical
completion remain separate. No monotonic instant is exported as cross-process
freshness authority. Queue includes `queue_id`, `target: {device_id, route, name}`,
`revision`, `generation`, `current_item_id`, `cancellation_requested`, `item_count`,
`items_truncated`, and up to 128 items with `item_id`, `provider`, `content_id`,
`kind`, `title`, `intent`. The first item and current item are retained when the
list is truncated. Extremely large individual projections return
`response_too_large` rather than silently dropping evidence.

## Bounds, identity and stop

A short registry lock covers only local submission/ticket bookkeeping, never task
I/O. Status bypasses it and reads the runner's cached snapshot. Stop sets the
runner's permanent cancellation latch immediately and has its reserved mailbox
slot, even while another command is blocked. Actual stop runs later on the owner
thread; already-dispatched remote commands cannot be retracted.

The registry retains up to `ticket_capacity` normal commands (default 128), one
reserved canonical stop, and up to `ticket_capacity` stop aliases. It never evicts
an accepted identity during this endpoint's lifetime. Once full, fresh identities
receive `history_full`; known identical retries and the first reserved stop remain
available. Different payloads under a known ID return `command_conflict`, including
metadata changes and stop-alias collisions. Repeated stop IDs coalesce to the
canonical `ticket_id`; always query the returned ID. Known aliases can also query
that ticket. More aliases than the explicit bound are rejected without dropping
the already-accepted stop.

A lost reply may follow successful local acceptance. Retry/query with the same
stable identity. `ticket_missing` is explicit absence from this endpoint's memory,
not evidence that the TV did nothing. The registry is not durable and a restarted
endpoint cannot deduplicate old identities; the durable executor supplies local
journal guarantees. Neither component promises exactly-once remote effects.

## Transport lifecycle and privacy

The existing runtime must belong to the current account; the server makes it 0700.
The endpoint is 0600. A private `ipc.lock` flock serializes endpoint creation and
cleanup, in addition to the runner's ownership locks. Symlinks/non-socket paths are
never clobbered. A live socket is never unlinked; a stale refused connection is
reclaimed only under the endpoint lease and after an inode check. Cleanup removes
only the socket inode this instance created. The client refuses non-private
runtime directories/endpoints. This is same-account local control, not a network
listener or cross-user authentication service.

At most `max_clients` non-daemon handlers run (default 8, allowed 2–64); each uses
a total input-frame deadline (default 1 second, at most 5) and bounded output wait.
Slow clients cannot acquire task ownership or hold its snapshot lock. Saturation
returns `server_busy` rather than allocating an unbounded queue/thread. This is
bounded responsiveness, not guaranteed admission under intentional saturation.
Each connection handles one request and closes. Client default timeout is 2
seconds. Unix socket path-length limits still apply; start fails safely if the
chosen runtime path is too long.

Shutdown asks the runner to cancel and waits only for the supplied bound. It does
not close the endpoint, task or backend from an IPC handler. `server.close()`
refuses while `owns_device` is true, leaving status accessible through a blocked
shutdown. Once ownership ends, close drains transport threads within its own bound;
a timeout retains the endpoint lease for a later close attempt. No implicit stop
is inferred from closing the transport.

## Offline verification

`tests/test_runner_ipc.py` covers strict codecs, safe historical projections,
concurrent duplicate submissions, mailbox/history bounds, reserved stop,
ID/payload/alias collisions, missing tickets, unknown outcomes and shutdown.
Fake frame connections prove total deadlines without sleeps. Real AF_UNIX tests
run only inside hermetic subprocesses whose audit hooks deny IPv4, IPv6 and DNS;
the default pytest socket/DNS guard is unchanged. These tests use temporary
synthetic runners, never device discovery, LAN traffic or hardware.
