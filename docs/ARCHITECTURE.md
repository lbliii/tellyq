# TellyQ architecture direction

Proposed after the first hardware proof, 2026-09-21. This is a staged design, not a
claim that these interfaces or dependencies have already been implemented.

The repository baseline now has typed JSON records, annotated package functions,
Ruff/ty/pytest, a uv lock and packaging/CI checks. The domain contracts and staged
layout below remain the next refactoring work; JSON boundary validation is still
limited to the current one-program experiment.

## Design decision

Keep the application policy independent of streaming services, control transports
and user interfaces. Implement capabilities honestly: the same physical receiver
may support very different controls and observations in different apps.

```mermaid
flowchart TD
    Cueby[Cueby planning] --> Commands[Typed application commands]
    Milo[Milo CLI and stdio MCP] --> Commands
    Web[Optional Chirp interface] --> Commands
    Commands --> Runner[One local session owner]
    Runner --> Policy[Pure state transitions and evidence rules]
    Runner --> Backend[PlaybackBackend protocol]
    Runner --> Store[SessionStore protocol]
    Backend --> YouTube[YouTube over Cast]
    Backend --> Other[Future proven service and transport adapter]
    Backend --> Fake[Deterministic test adapter]
    Runner --> ReadModel[Immutable status snapshots]
    ReadModel --> Milo
    ReadModel --> Web
```

A provider identifies the content ecosystem; a transport controls a device.
The first adapter combines YouTube and Cast. Do not force every backend into a
provider × transport class hierarchy before a second implementation needs it.
Catalog search, title resolution and playback are separate concerns.

## Types and contracts

Use standard Python 3.14 types, `Protocol`, enums, and frozen/slotted dataclasses.
Frozen dataclasses are only shallowly immutable: shared collections must be tuples,
frozensets or owned read-only snapshots. Validate unknown external data once at
the boundary; use typed values within the application.

| Contract | Meaning / required information |
| --- | --- |
| `ContentRef` | Provider plus opaque provider content ID, content kind and optional display metadata. A series or search result is not an exact episode. |
| `PlaybackTarget` | Stable device identity and chosen adapter/route. Names are presentation; network addresses are local configuration. |
| `PlaybackCapabilities` | Per-route support for exact launch, pause/resume, stop, identity, progress, completion and display/power evidence, with verified/unsupported/unknown status and provenance. |
| `PlaybackRequest` | Content, target, queue item, request ID, attempt ID and applicable policy. Request IDs support deduplication. |
| `CommandReceipt` | Accepted/rejected/unknown, timestamps and structured error. Acceptance is independent of observed playback. |
| `PlaybackObservation` | Provider/device/session identity, observed fields, source, time, sequence/connection generation and freshness. Missing fields remain unknown. |
| `SessionSnapshot` | Queue position, execution state, latest evidence and pending action. Safe to share with readers. |
| `DisplayEvidence` | Separate timestamped user confirmation or verified display signal. A historical visual confirmation never proves current TV power/input. |

Initial ports:

- `PlaybackBackend`: describe capabilities, start an exact request, obtain
  observations and stop an owned session. Pause/seek use separate supported
  capabilities rather than success-shaped no-ops. Library-specific objects stay
  inside the adapter; YouTube content parsing is not a shared domain utility.
- `SessionStore`: load and atomically save versioned queue/session/attempt state
  with revision checks. Also persist command intent and outcome for recovery.
- `Clock`: UTC timestamps for records and monotonic deadlines for live waits.
  Persisted monotonic values are not comparable across host restarts.
- `Catalog` / `ContentResolver`: add at M6 when there is a real catalog consumer;
  return known exact references plus availability evidence, not guessed watch URLs.

Typed application methods can be exposed through Milo without making Milo types
part of the domain. Generate input and output wire contracts from annotated command
types using the selected Milo version. Prove supported dataclass/TypedDict shapes
in the adoption spike; keep one central codec where conversion is required.
Do not hand-maintain separate CLI and MCP schemas.

## Three kinds of state

1. **Queue intent:** pending, current, completed, skipped, canceled or needs attention.
2. **Observed playback:** unknown, idle, buffering, playing, paused or ended, tied to
   a particular receiver session and content identity.
3. **Command execution:** pending, dispatched, acknowledged, rejected or uncertain.

An acknowledged start can coexist with unknown playback. A user stop cancels
advancement; it is not natural completion. An app disappearing may indicate stop,
failure or takeover. Missing metadata is not an empty queue or a finished episode.
Provider autoplay to a different title is not automatically TellyQ's next item.

Normalize raw adapter messages without inventing fields. A pure reducer then maps
`state + event -> next state + requested effects`. Clocks, sleeps, disk/network I/O
and generated IDs live outside the reducer. Completion policy consumes correlated,
fresh evidence; the runner executes effects and persists the resulting decisions.

Errors have stable codes, a useful message, an operation/attempt ID, an uncertainty
indicator and a suggested recovery step. Examples include `device_unavailable`,
`auth_required`, `unsupported_capability`, `session_replaced`, `observation_stale`
and `command_outcome_unknown`. Retryability is not permission to repeat a possibly
successful start. Inspect the receiver before retrying uncertain effects.

## Ownership, persistence and cancellation

The present process-wide lock protects short experiments, but a lock held over a
whole program would prevent useful stop/status commands. M2 introduces one local
session runner with a command mailbox and immutable read snapshots. Device effects
are serialized; callback threads enqueue observations. Each device client and its
request session have a clear owner. No framework request handler shares a mutable
PyChromecast controller directly.

Use one foreground `tellyq serve` process initially, with a private local IPC endpoint
for short-lived CLI/MCP clients. The IPC envelope reuses application command and
result types. Closing an MCP client does not kill an active TV program. Starting a
second owner fails clearly. Define shutdown, cancellation and operator stop before
adding automatic background startup. No recurring agent wakeups are needed.

Keep status reads responsive during network calls. Bound all I/O and observation
windows; do not hold persistence locks while waiting on the network. A stop request
cancels queued advancement immediately and is dispatched by the device owner as
soon as any bounded in-flight operation allows. Measure the delay under faults.

Keep JSON for M1's small single-writer state. At M2, prefer a local SQLite store
for atomic queue position, attempt and command-intent updates in one transaction.
Migrate existing JSON explicitly and retain readable JSON run reports. SQLite
transactions cannot atomically commit a remote TV command: reconcile the uncertain
gap after restart. Do not automatically resend it. Journal only useful decisions
and observations; a general event-sourcing platform is unnecessary.

All addresses, session credentials, pairing material and detailed histories stay
under ignored runtime state. Committed fixtures use synthetic identifiers and
sanitized messages. Public error output must not contain tokens or credential URLs.

## What to reuse from the existing Python stack

This review used the local Chirp checkout and its installed Milo 0.4.3 / Kida 0.12.0
code, plus the public Kida and Milo repository documentation and MCP router. No
changes were made to those projects and their full suites were not rerun.

| Project / evidence inspected | Adopt in TellyQ | Scope |
| --- | --- | --- |
| [Kida immutable tokens](https://github.com/lbliii/kida/blob/main/src/kida/_types.py) and [sharing contract](https://github.com/lbliii/kida/blob/main/site/content/docs/about/thread-safety.md) | Frozen value objects, explicit ownership, bounded sharing guarantees and actual GIL-disabled tests | Engineering patterns now; rendering through Milo/Chirp only when needed |
| [Milo protocols](https://github.com/lbliii/milo-cli/blob/main/src/milo/_protocols.py), [types](https://github.com/lbliii/milo-cli/blob/main/src/milo/_types.py), [MCP router](https://github.com/lbliii/milo-cli/blob/main/src/milo/_mcp_router.py) and [test layers](https://github.com/lbliii/milo-cli/blob/main/docs/testing.md) | Small structural protocols, pure state transitions, one typed command definition, schema/dispatch/transport parity | Recommend adopting Milo for CLI + local MCP in M3, after a pinned-version integration test |
| [Chirp typed command handlers](https://github.com/lbliii/chirp/blob/main/src/chirp/cli/_milo_handlers.py), [tool registry](https://github.com/lbliii/chirp/blob/main/src/chirp/tools/registry.py) and [state snapshots](https://github.com/lbliii/chirp/blob/main/src/chirp/app/state.py) | Setup-time registration, stable runtime views, separate framework adapters and executable contracts | Optional web extra in M7; playback never depends on an ASGI server |

Milo is an especially good fit because its MCP discovery/dispatch already uses the
registered command contracts and supports structured results. Its own tests are
not proof that TellyQ's hardware effects are correct: add application parity and
real-client tests. Chirp also exposes tools, but do not build two independent MCP
registries. A future hosted surface must project the same application commands.

Use focused local modules for clocks/deadlines, JSON serialization/migrations,
diagnostics and runtime paths. Extract a utility only after concrete callers share
its semantics. Avoid a generic `utils.py`, service locator, plugin marketplace or
new shared ecosystem package for this application.

## Python and quality gates

Keep Python 3.14 with the standard GIL build as the supported initial runtime.
Threading is valuable for I/O ownership and observation responsiveness regardless
of the GIL. Add a 3.14t lane that checks `sys._is_gil_enabled()` after imports and
runs concurrent command/event tests against actual dependency versions. Do not
force an unsupported extension to run without the GIL. Kida/Milo's support does
not establish PyChromecast, protobuf or zeroconf thread safety.

Borrow the stack's Ruff/Ty/pytest workflow, dependency pinning and explicit public
contracts. Use unit tests for pure policy, replay tests for receiver traces, adapter
contract tests, CLI/MCP parity tests and explicitly opted-in live hardware tests.
Test behavior across boundaries, not only mock call counts. Add tests that prove a
contract check fails when the contract is intentionally broken.

Stage the package as `domain/`, `application/`, `adapters/`, `interfaces/` and
`infrastructure/` under `src/tellyq/` as each responsibility appears. Dependencies
point inward. Domain/policy imports neither PyChromecast nor Milo nor Chirp; imports
of the core do not open sockets, discover devices or create runtime directories.
