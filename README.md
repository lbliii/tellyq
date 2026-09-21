# TellyQ

A personal TV programmer: describe a mood, then let Cueby, your TV-programming companion, choose and play a mix of familiar favorites and adjacent discoveries.

TellyQ is the app; Cueby is its companion.

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
receiver response and left stale `playing` state. Application integration and this
stop-verification fix are in [PR #9](https://github.com/lbliii/tellyq/pull/9) and
[PR #8](https://github.com/lbliii/tellyq/pull/8). Their combined candidate passed
351 tests plus type, lint, build and installation checks. Merge and a fresh
hardware regression remain pending; **M1 is not yet accepted**. See the
[M1 acceptance record](docs/M1-ACCEPTANCE.md) for separate code, CI and hardware gates.

Read [the MVP plan](docs/MVP.md) and [research notes](docs/RESEARCH.md) before implementation. An example one-item queue is in [examples/queue.json](examples/queue.json).

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

`queue` only writes the local one-item queue. `start` plays it and observes for
30 seconds, then exits while playback continues. `status` obtains fresh events
without starting or resuming anything. `stop` exits the saved YouTube receiver
session and attempts to verify that the app has exited. It does not pause the video;
a return to the Chromecast home screen is an expected visible result. It refuses
to stop a replacement session or known different content. An explicit `queue` permits a new attempt;
accidental repeated `start` calls do not restart an active/uncertain attempt.

All commands emit JSON. `commands[].returned` records the command result;
`evidence.receiver_playback_confirmed` requires the requested content ID, PLAYING
state, and two advancing positions from the same media session. It does **not**
prove the TV is displaying the video. Observations retain unknown fields as null.
Exit code 0 means the operation completed; inspect the evidence/state for its
outcome. A timeout may leave playback unconfirmed, so inspect status before retrying.

The M1 integration candidate adds `schema_version: 1` to reports and applies the
stricter domain evidence policy: unknown ad state keeps `state: unconfirmed`, even
when `observed_state: playing` and fresh identity/progress are reported. That
result does not contradict visible playback; it avoids claiming evidence the
receiver did not supply. Existing queue/session files remain compatible. New
snapshots preserve ownership/history while discarding old monotonic evidence on
process restart. Final adoption is tracked in the acceptance record above.

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

See [implementation work and verification](docs/IMPLEMENTATION.md). No automatic
queue advancement or full-episode completion test has been performed.

## Longer-term direction

Theme-based programming across Netflix, Disney+, Prime Video, and Apple TV, with reliable queue advancement and a balance of comfort viewing and discovery. The initial YouTube experiment proves the playback connection; it does not establish support for those subscription services.
