# M1 persistence and boundary validation

This stream hardens the existing one-program JSON state without changing its
on-disk shape. It does not add the runner, automatic advancement, a database or
the future typed-store adapter.

## Boundary and compatibility

`read()` and `save()` validate files named `queue.json` and `session.json` before
returning or replacing state. This filename dispatch is deliberately narrow: the
current controller already reads those files before opening a device connection,
including for `status`, `stop` and `probe`. Other JSON objects, including run
reports, retain their generic shape. Renaming arbitrary data to a reserved state
filename opts it into that schema; this is not content-based schema inference.

Queues retain `schema_version: 1`, one supported YouTube episode, every existing
queue state, and optional `last_report`. Required fields receive type and value
checks: the target must be a UUID; timestamps must include a timezone; item IDs
and display text must be nonempty strings; provider/content IDs must identify the
supported program; the URL must be its HTTPS YouTube watch URL; and state must be
one of `queued`, `starting`, `playing`, `failed`, `unconfirmed`, `stopped`, or
`finished`. Additional URL query parameters are allowed when they preserve its
single video ID. The supported contract is still the current one-program MVP.

Original unversioned session records remain readable and are not rewritten into
a new format. They require target UUID, the supported content ID, and nonempty
app and app-session IDs. Missing ownership identities fail closed. An explicit
`schema_version: 1` is accepted with the same fields; any other version fails.
Booleans and floating-point numbers do not count as integer schema versions.

Unknown fields are rejected instead of being silently discarded during typed
reconstruction. Future schema versions fail explicitly. There is no migration in
this change: known version-1 data stays version 1, and unsupported or damaged
state stays on disk for investigation. `load_queue()` constructs the existing
`QueueDocument` contract from checked fields, without unchecked TypedDict casts.
An absent queue still returns the familiar instruction to queue the program.

Malformed UTF-8, invalid JSON, duplicate object keys, non-finite numeric literals
and numbers that overflow to infinity are rejected. Error messages name the
file/field and requirement without reproducing private values. Missing files
return `None`; corrupt state does not masquerade as missing state. Serialization
also rejects non-finite numbers rather than writing nonstandard JSON.

## Writes and ownership

Writes use a unique temporary file beside the destination, mode `0600`, UTF-8,
flush and file `fsync`, then atomic replacement. New state directories and lock
files use `0700` and `0600`, respectively. Existing directory permissions are
left unchanged. A serialization failure, file-sync interruption or rename
failure before replacement leaves the old document intact and cleans the
temporary file during normal Python unwinding.

This is atomic file replacement, not a transaction across files or devices. No
directory `fsync` is performed, so power-loss durability of the replacement is
not promised. A killed process cannot run its cleanup and may leave a private
temporary file, which is never read as live state. These guarantees do not imply
that a remote command and its saved session commit together.

The existing nonblocking `fcntl` command lock still permits only one local
command owner and releases when the context exits, including after a command
exception. This remains a macOS/Linux process lock, not the M2 session-owner
mailbox design. Tests prove exclusion using a second Python process.

## Dependency boundaries and verification

`tellyq.clock.SystemClock` supplies aware UTC datetimes and monotonic interval
values. `tellyq.programs` holds the configured example program's metadata.
Persistence imports neither PyChromecast nor the Cast adapter; a subprocess test
blocks Cast dependencies and networking while importing and constructing a queue.
Importing these modules does not create runtime files. The adapter's existing
example constant remains temporarily duplicated until the later integration PR.

The storage tests cover supported version-1 states, unchanged legacy sessions,
required and optional field validation, future-version rejection, safe error
messages, generic report compatibility, corrupted state failing before controller
connection, private atomic writes, injected failures and process locking. Run
`uv run --locked poe check` and `uv run --locked poe preflight` to validate the
stream alongside the existing controller/CLI/evidence regressions. All receiver
behavior here is offline; there is no new hardware result.
