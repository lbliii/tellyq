# TellyQ

A personal TV programmer: describe a mood, then let Cueby, your TV-programming companion, choose and play a mix of familiar favorites and adjacent discoveries.

TellyQ is the app; Cueby is its companion.

**M2 is closed with a known reliability limitation.** The local Python 3.14
controller now supports an explicit YouTube lineup, verified native handoffs,
start/status/pause/resume/stop, durable history and bounded reconnect recovery.
Three of four recent three-item attempts completed. The intermittent enqueue
stall remains open as [issue #42](https://github.com/lbliii/tellyq/issues/42).
See the [M2 closeout](docs/M2-ACCEPTANCE.md) for the complete evidence and limits.
M3's Milo/MCP foundation is implemented as a local stdio surface with
`start`, `status`, and `stop`. Its supervised hardware checkpoint passed with
receiver evidence and separate user visual confirmation; broader CLI/Python/MCP
parity remains open. See the [M3 MCP checkpoint](docs/M3-MCP-SECOND-CHECKPOINT.md).

## First playback proof (M0)

Queue and start one real program on the living-room Chromecast through software commands, then verify playback and stop control.

- Service: YouTube.
- Program: [Bob Ross — Autumn Fantasy (season 20, episode 7)](https://www.youtube.com/watch?v=FozIp7Va7dY), from the official channel.
- Controller: a small local Python program using PyChromecast.
- Interface: structured commands for queue, start, status, and stop. Add MCP after the playback connection works.

The first playback proof (M0) passed on 2026-09-21. The user confirmed visible
Bob Ross playback and automatic TV switching after connecting Chromecast HDMI.
Receiver telemetry reported the exact episode ID/title and advancing position.
Receiver playback remains separate from evidence that the TV is showing the program.

M1 builds a maintainable core around that proof. Its domain contracts, validated
storage and Cast normalization are merged. The merged checkpoint at `a2b72a9`
passed 251 automated tests and packaging checks. A live regression confirmed
visible playback and status without a restart. Stop returned the TV to the
Chromecast home screen, as intended, but software failed to recognize the idle
receiver response and left stale `playing` state. Application integration and the
stop-verification fix have since merged through
[PR #9](https://github.com/lbliii/tellyq/pull/9) and
[PR #8](https://github.com/lbliii/tellyq/pull/8). Actual `main` at `71a3c39` passed
351 tests plus type, lint, build, installation and macOS/Linux CI checks. The fresh
hardware regression also passed: visible Bob Ross, two status reads without a
restart, verified receiver-app exit, user-confirmed Chromecast home screen and
persisted `stopped` state. **M1 passed at `71a3c39`.** Unknown ad state still limits
strict receiver playback proof; see the
[M1 acceptance record](docs/M1-ACCEPTANCE.md) for the separate evidence and limits.

M2's first batch is merged at `7a08929`: continuous lifecycle capture/replay,
guarded pause/resume commands and the SQLite queue/journal foundation. Its merged
checkpoint passed 487 automated tests and packaging checks. Three live runs across
two titles produced ending candidates, but initially failed pause/resume and
completion acceptance. The repairs merged at `e4b2a91`, which passed 531 tests.
The repaired live diagnostic verified pause/resume and cleanup stop; the user also
confirmed visible pause. Two focused metadata runs
identified a YouTube-specific numeric state field, with first-party source evidence
for separate ad states. Duration was already available. The
[metadata investigation](docs/YOUTUBE-METADATA.md) supplied the basis for a finite
YouTube state interpreter and ordered content attribution. The
[completion batch](docs/M2A-COMPLETION-PLAN.md) adds those contracts, historical
completion reporting and a repeatable single-video checkpoint runner. Reviewed
candidate `2599345` passed 666 offline tests and three live natural endings across
two titles, including verified and visible pause/resume. It also retained completion
through a later ad state and content replacement while refusing unsafe cleanup.
PRs #20–22 are merged at `6aec896`; the merged tree matches the tested candidate
and passes 666 tests. The [M2a checkpoint](docs/M2A-CHECKPOINT.md) preserves every
attempt and remaining limits. The [M2b plan](docs/M2B-PLAN.md) starts the persistent
owner, queue policy and durable execution work. Those components merged at
`9715b58`, which passes 832 offline tests. The composition adds an explicit
[foreground service](docs/FOREGROUND-SERVICE.md), private local commands and guarded
queue handoffs, merged at `b4ff7d3` with 965 passing offline tests. The
[foreground-service checkpoint](docs/M2B-CHECKPOINT.md) verified playback, durable
natural completion, cancellation, stop and a status-only reopen. The successor
held, and one active-stop measurement took 10.431 seconds against a ten-second
target. The subsequent [closing workstreams](docs/M2B-PLAN.md#closing-workstreams)
addressed diagnostics, responsiveness and recovery acceptance. Earlier unexplained
YouTube code-5/reset behavior retains its protective hold.

Read [the MVP plan](docs/MVP.md) and [research notes](docs/RESEARCH.md) before implementation. An example one-item queue is in [examples/queue.json](examples/queue.json).

The optional [native YouTube runner](docs/NATIVE-QUEUE-RUNNER.md) now connects the
durable queue to receiver-managed successors. It reserves one next item, verifies
its playback before adoption, and reconciles authorized playback on reconnect
without sending new work. It is opt-in through `"mode": "native"`; existing
manifests retain legacy behavior. The [merged-runner checkpoint](docs/NATIVE-RUNNER-CHECKPOINT.md)
passed one three-item sequence, active stop and reconnect, but a second sequence
held after its middle item. A subsequent [enqueue investigation](docs/ENQUEUE-INVESTIGATION.md)
passed repeated and distinct three-item comparisons with four more verified
handoffs, but did not reproduce or explain the stall. Those successes count in the
[qualified M2 closeout](docs/M2-ACCEPTANCE.md); the stall remains an open defect.
The examples use quiet nature clips; the user confirmed
visible advancement and audible nature sound in the distinct-video investigation.

For the next stages, see the [measurable milestone roadmap](docs/ROADMAP.md) and
[architecture direction](docs/ARCHITECTURE.md). They cover typed contracts,
reliable queueing, Milo CLI/MCP, subscription-service experiments, Cueby's
programming logic and an optional Chirp interface. These are planned milestones;
the implemented behavior is described below.

## Run locally

Requires Python 3.14 on macOS/Linux, uv, and a reachable Chromecast on the same home network. Connect
the Chromecast to TV HDMI and keep the Sonos Beam on HDMI ARC. The TV may need to
be turned on and switched to the Chromecast input manually if HDMI-CEC is disabled.

```sh
uv sync --locked
uv run --locked tellyq discover
uv run --locked tellyq queue --device UUID_FROM_DISCOVER
uv run --locked tellyq start
uv run --locked tellyq status
uv run --locked tellyq stop
```

The optional M3 stdio MCP surface uses the same already-running foreground
owner. Run the owner in one terminal and launch Milo in a second terminal with
the owner's absolute private runtime; the MCP caller cannot supply a runtime
path or create a second playback owner:

```sh
# Terminal 1, from the TellyQ checkout.
mkdir -p runtime/mcp-owner
cp examples/native-session.json runtime/mcp-session.json
# Edit runtime/mcp-session.json with a fresh queue_id and discovered device UUID.
uv run --locked tellyq --runtime "$PWD/runtime/mcp-owner" serve \
  --manifest "$PWD/runtime/mcp-session.json"
```

```sh
# Terminal 2, from any working directory; use the absolute checkout path.
TELLYQ_OWNER_RUNTIME="/absolute/path/to/tellyq/runtime/mcp-owner" \
  uv --directory "/absolute/path/to/tellyq" run --locked --extra mcp tellyq-mcp --mcp
```

MCP exposes only `start`, `status`, and `stop`. An accepted `start` ticket is
not verified playback; use `status` for timestamped evidence. Stopping the MCP
process does not stop the separate foreground owner. See [the M3 MCP
foundation](docs/M3-MCP.md) for the remaining parity and live checklist.
For a regular CLI call that requires this same owner contract, pass `--owner`
after `start`, `status`, or `stop`. It returns the shared structured result and
fails safely if the owner is unavailable; it never starts direct playback.

`queue` only writes the local one-item queue. `start` plays it and observes for
30 seconds, then exits while playback continues. `status` obtains fresh events
without starting or resuming anything. `stop` exits the saved YouTube receiver
session and attempts to verify that the app has exited. It does not pause the video;
a return to the Chromecast home screen is an expected visible result. It refuses
to stop a replacement session or known different content. An explicit `queue` permits a new attempt;
accidental repeated `start` calls do not restart an active/uncertain attempt.

All commands emit JSON. `commands[].returned` records the command result;
`evidence.receiver_playback_confirmed` requires the requested content ID, PLAYING
state, two advancing positions from the same media session, and qualified inactive
ad evidence. The YouTube adapter can supply that evidence from reviewed provider
states with fresh receiver identity and consistent Cast telemetry; missing break
metadata alone remains unknown. This does **not** prove the TV is displaying the
video. Observations retain unknown fields as null.
Exit code 0 means the operation completed; inspect the evidence/state for its
outcome. A timeout may leave playback unconfirmed, so inspect status before retrying.

The M1 integration adds `schema_version: 1` to reports and applies the
stricter domain evidence policy: unknown ad state keeps `state: unconfirmed`, even
when `observed_state: playing` and fresh identity/progress are reported. That
result does not contradict visible playback; it avoids claiming evidence the
receiver did not supply. Existing queue/session files remain compatible. New
snapshots preserve ownership/history while discarding old monotonic evidence on
process restart. The acceptance record above documents the observed hardware outcome.

`probe --device UUID_FROM_DISCOVER` is the direct playback experiment that bypasses
the persistent queue. Both `probe` and `start` accept `--seconds 5..120` to change
the observation window. `status` and `stop` accept an explicit `--device`; otherwise
they use the saved session, then the queue. Only one command may run at a time.

Device identity, session information, queue state, diagnostics and timestamped run
reports stay under ignored `runtime/` in the working directory. Run commands from
the repository root to reuse its saved queue/session. Ordinary Python 3.14 threads run discovery
and Cast communication; a synchronized queue transfers observation snapshots to
the controller. This setup uses the GIL-enabled interpreter, not the free-threaded
build. Runtime JSON includes the interpreter version and actual GIL status.

## Development

```sh
uv run --locked poe check      # Ruff, formatting, ty, offline tests
uv run --locked poe preflight  # Also build and verify an isolated wheel install
```

The lock includes development tools. Normal tests block network access and never
control the TV. See [contributor guidance](CONTRIBUTING.md) for coverage, optional
commit hooks, dependency changes and the macOS/Linux CI checks. For a sandbox-local
uv cache, prefix uv commands with `UV_CACHE_DIR=runtime/uv-cache`.

See [implementation work and verification](docs/IMPLEMENTATION.md) and the
[commit-specific completion checkpoint](docs/M2A-CHECKPOINT.md). The first
[foreground-service checkpoint](docs/M2B-CHECKPOINT.md) held before advancement;
successful live automatic queue advancement remains unverified.

## Longer-term direction

Theme-based programming across Netflix, Disney+, Prime Video, and Apple TV, with reliable queue advancement and a balance of comfort viewing and discovery. The initial YouTube experiment proves the playback connection; it does not establish support for those subscription services.
