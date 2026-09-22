# TellyQ

A personal TV programmer: describe a mood, then let Cueby, your TV-programming companion, choose and play a mix of familiar favorites and adjacent discoveries.

TellyQ is the app; Cueby is its companion.

**M2 is closed with a known reliability limitation.** The local Python 3.14
controller now supports an explicit YouTube lineup, verified native handoffs,
start/status/pause/resume/stop, durable history and bounded reconnect recovery.
Three of four recent three-item attempts completed. The intermittent enqueue
stall remains open as [issue #42](https://github.com/lbliii/tellyq/issues/42).
See the [M2 closeout](docs/M2-ACCEPTANCE.md) for the complete evidence and limits.
M3's Milo/MCP surface exposes `start`, `status`, and `stop` for the existing
foreground owner. Its supervised hardware checkpoint passed with receiver
evidence and separate user visual confirmation. CLI, Python, Milo shell, and
MCP now share the result envelope; the advertised MCP schema describes its
stable fields while leaving the nested owner response opaque. See the
[M3 acceptance record](docs/M3-ACCEPTANCE.md).
M4's Netflix probe classified that route as assisted app launch. Plex is now the
selected second-provider candidate because its server exposes library identity and
playback sessions. The [first Plex checkpoint](docs/M4-PLEX-PROBE.md) found no
local server or launchable native TV client and stopped at the trusted-install,
account, and test-library setup gate before adapter work.

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
reliable queueing, Milo CLI/MCP, second-provider experiments, Cueby's
programming logic and an optional Chirp interface. These are planned milestones;
the implemented behavior is described below.

## Run locally

Requires Python 3.14 on macOS/Linux, uv, and a reachable Chromecast on the same home network. Connect
the Chromecast to TV HDMI and keep the Sonos Beam on HDMI ARC. The TV may need to
be turned on and switched to the Chromecast input manually if HDMI-CEC is disabled.

```sh
uv sync --locked
uv run --locked tellyq discover
mkdir -p runtime/owner
cp examples/mcp-checkpoint.json runtime/my-session.json
# Edit runtime/my-session.json with a fresh queue_id and exact discovered device UUID.
uv run --locked tellyq --runtime "$PWD/runtime/owner" serve \
  --manifest "$PWD/runtime/my-session.json"
```

With the owner running, use another terminal in the same checkout:

```sh
uv run --locked tellyq --runtime "$PWD/runtime/owner" start
uv run --locked tellyq --runtime "$PWD/runtime/owner" status
uv run --locked tellyq --runtime "$PWD/runtime/owner" stop
uv run --locked tellyq --runtime "$PWD/runtime/owner" shutdown --timeout 5
```

To use the optional M3 stdio MCP surface instead of the CLI controls above, keep
the owner running and launch Milo with its absolute private runtime. An MCP
caller cannot select a different runtime or create another playback owner:

```sh
TELLYQ_OWNER_RUNTIME="/absolute/path/to/tellyq/runtime/owner" \
  uv --directory "/absolute/path/to/tellyq" run --locked --extra mcp tellyq-mcp --mcp
```

MCP exposes only `start`, `status`, and `stop`. An accepted `start` ticket is
not verified playback; use `status` for timestamped evidence. Stopping the MCP
process does not stop the separate foreground owner. See [the M3 MCP
foundation](docs/M3-MCP.md) for its command contract and checkpoint workflow.
Normal `start`, `status`, and `stop` CLI calls require the foreground owner and
return the shared structured result. An unavailable owner is an error; no
second playback controller is started.
The `tellyq-mcp` entry point accepts those commands in a shell as well, returning
the same JSON result and a nonzero exit status on failure.

`queue` only writes a one-item local queue without playback; a normal owner
session uses a manifest. `start` submits the first manifest item and returns a
ticket while the owner continues monitoring.
`status` reads the owner's timestamped evidence without relaunching playback.
`stop` requests guarded receiver stop and may return the TV to the Chromecast
home screen. Accepted commands do not prove their receiver effects; inspect
status or the ticket before retrying an uncertain outcome.

All commands emit JSON. Owner controls use the shared result envelope with a
ticket or timestamped snapshot in `response`. Direct diagnostic reports use
`commands[].returned` for the command result.
`evidence.receiver_playback_confirmed` requires the requested content ID, PLAYING
state, two advancing positions from the same media session, and qualified inactive
ad evidence. The YouTube adapter can supply that evidence from reviewed provider
states with fresh receiver identity and consistent Cast telemetry; missing break
metadata alone remains unknown. This does **not** prove the TV is displaying the
video. Observations retain unknown fields as null.
Exit code 0 means the local command succeeded; inspect the evidence/state for
the receiver outcome. A timeout may leave playback unconfirmed, so inspect
status before retrying.

The M1 integration adds `schema_version: 1` to reports and applies the
stricter domain evidence policy: unknown ad state keeps `state: unconfirmed`, even
when `observed_state: playing` and fresh identity/progress are reported. That
result does not contradict visible playback; it avoids claiming evidence the
receiver did not supply. Existing queue/session files remain compatible. New
snapshots preserve ownership/history while discarding old monotonic evidence on
process restart. The acceptance record above documents the observed hardware outcome.

`probe --device UUID_FROM_DISCOVER` remains an explicit, direct diagnostic that
bypasses the owner and persistent queue. Only `probe` accepts `--seconds 5..120`
to change its observation window. Normal playback controls require the owner.

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
