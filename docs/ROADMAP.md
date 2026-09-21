# TellyQ roadmap

Planning baseline: 2026-09-21. M0 is verified. M1's repository tooling and initial
type annotations are implemented; its full domain contracts remain in progress.
The later milestones below are proposed work.

## Product goal

Describe a mood and let Cueby assemble a program of familiar favorites and adjacent
discoveries. TellyQ plays that program on the existing TV, reports what it actually
observes, and handles interruption without silently losing or repeating items.

The smallest useful release delivers this on YouTube. Cross-service programming
adds a subscription service only after its launch and observation capabilities
are proven on the actual device. A graphical interface is optional.

## Milestone scoreboard

| ID | Outcome | Completion evidence | Status / dependency |
| --- | --- | --- | --- |
| M0 | One real program under software control | Exact Bob Ross ID/title and advancing position; user confirms visible playback and automatic TV switching; observed YouTube exit after stop | **Passed** |
| M1 | Typed core with explicit contracts | Existing behavior preserved; core runs against real and fake adapters; schemas, errors and state transitions have contract tests; lint/type/tests pass | In progress: tooling + initial annotations; M0 |
| M2 | Reliable unattended YouTube queue | Natural endings observed; three sessions of three items advance correctly; pause, stop, disconnect and restart scenarios produce no false completion or duplicate launches | M1 |
| M3 | Cueby controls the same functions through MCP | CLI/Python/MCP parity, real stdio handshake and one live start/status/stop session pass; long playback outlives individual tool calls | M2 |
| M4 | Evidence-based subscription-service decision | Device/service/control-route matrix; bounded experiments; choose a supported route or document a specific blocker | Research starts now; live probes after M1 |
| M5 | One subscription service integrated | Two exact titles, three start/status/stop runs, and two cross-service handoffs pass the supported autonomy level | M2 + M4 |
| M6 | Mood becomes a useful evening of TV | Ten planning scenarios satisfy constraints; user accepts three real lineups; every item resolves to a known provider ID and supported control route | M3; YouTube can ship before M5 |
| M7 | Optional Chirp interface | Queue, now-playing evidence and controls agree with CLI/MCP; live updates reconnect; browser controls do not create another playback owner | M3 + M6 |
| M8 | Dependable personal release | Three evenings of at least 60 minutes, ten verified handoffs, restart recovery, no duplicate starts or false completion, reproducible install and troubleshooting | M2 + M3 + M6; M5 for cross-service claims |

Counts above are release gates, not claims of statistical reliability. All failures
stay in the denominator and in the run record. M7 is not a prerequisite for M8.

## M1 — Make the successful experiment a maintainable core

The [M1 execution plan](M1-PLAN.md) breaks this milestone into independent agent
workstreams, file ownership, PRs targeting `main`, and a later integration gate.

Deliver frozen typed models for content, targets, attempts, acknowledgements,
observations, capabilities and errors. Move YouTube ID parsing and Cast app/session
handling into the adapter. Inject the clock and state store. Keep Bob Ross as an
example/fixture instead of a controller-wide constant.

Introduce only the protocols needed by the existing backend and a test double.
Use the design in [ARCHITECTURE.md](ARCHITECTURE.md), including separate playback,
queue and display-evidence state. Preserve unknown ad/position values rather than
manufacturing certainty. Validate serialized input at the boundary.

Acceptance:

- Existing 16 regressions continue to pass or have equivalent migrated assertions.
- A fake backend passes the same adapter contract as the Cast backend's recorded
  behavior, without importing PyChromecast into the domain or storage layers.
- Partial, stale, duplicate, out-of-order and replaced-session observations never
  become new playback or completion evidence.
- CLI command names remain compatible; JSON has a documented version and an
  explicit migration story. Old local queue files can be read/migrated safely.
- Adopt Ruff formatting/linting, Ty, pytest and a reproducible development lock.
  Core tests run with the Cast and future UI/MCP dependencies absent.

## M2 — Finish a program, then advance exactly once

Split into two small deliverables.

**M2a: prove lifecycle signals.** Observe three natural YouTube endings across at
least two real titles. At least one run includes a deliberate pause/resume. Record
how ads, buffering, errors, manual stop, app exit and autoplay appear. Seeking near
the end may help diagnosis but does not replace natural-end acceptance. Capture
sanitized fixtures; synthetic ad cases are labeled separately if no live ad occurs.

**M2b: add a persistent session runner.** One local process owns playback state and
device commands. It monitors observations after CLI calls return, persists queue
transitions, and accepts stop/status while an item is running. Add explicit pause,
resume and skip only for adapters that support them. Record skipped separately
from finished. Handle provider autoplay without competing with it.

Acceptance:

- Three three-item runs yield six correct automatic handoffs, with no early or
  duplicate advancement. Record actual transition delays and observation gaps.
- A replay suite covers an ad crossing expected end time, long pause, buffering,
  dropped connection, duplicate terminal event and another controller taking over.
- Kill/restart at pre-dispatch, post-dispatch/pre-acknowledgement and post-completion
  points. Reconcile uncertain effects before retrying; never promise transactional
  exactly-once delivery to a device that cannot supply it.
- Stopping cancels pending advancement. Status is non-mutating at the device.
  On a healthy LAN, local stop requests are accepted within one second and the
  observed stop outcome arrives within ten seconds. A timeout is explicit.
- Missing completion evidence holds the queue and requests attention. Published
  runtime and reaching a duration budget never imply completion.

## M3 — Adopt Milo for CLI and MCP

Use Milo command registration around the typed application service. Keep the
existing CLI vocabulary and add session identifiers where needed. Start with
local stdio MCP; a hosted gateway and web server are unnecessary here.

Acceptance:

- One command definition owns input validation, defaults and schema. CLI, direct
  Python dispatch and MCP produce equivalent outcomes and structured errors.
- `milo verify` succeeds, plus TellyQ's subprocess tests for initialize, tools/list,
  tools/call, bad inputs, stdout cleanliness and server exit during playback.
- `start` returns a request/session handle; `status` exposes timestamped evidence;
  `stop` remains callable while playback monitoring continues.
- Expose only intended tools. Mark device-read operations accurately and mutation
  hints conservatively. Hints do not replace session ownership or capability checks.
- One hardware start/status/stop run through an actual MCP client passes. The
  server may exit without silently abandoning the separately owned runner.

## M4 — Choose the next service by experiment

First identify the Chromecast generation, whether Android/Google TV is present,
the Samsung model, existing apps, account tier and available control endpoints.
Keep addresses and pairing data local. App launch is only one column in the matrix.

Recommended investigation order:

1. Netflix is the first **short feasibility probe**, because the user has already
   reported Google Assistant launching it on this setup. This is a useful lead,
   not proof of a Python API or exact-episode control.
2. Compare Disney+ and Prime Video using the same checks; test the one with an
   identifiable software launch route first. Neither is yet established as easier.
3. Include Apple TV in the matrix: Apple now documents Android-to-Cast playback.
   The phone's Cast UI alone does not establish Mac/Python control.

Budget at most one focused research pass and one supervised hardware session per
promising route before a decision. A route with no callable authorized launch path
can be recorded as blocked without spending a session repeating app launches.
Do not continue an indefinite compatibility hunt.

For every service × device × route, record exact-title launch, identity, state,
progress, completion, stop, auth/setup effort, takeover handling and test evidence.
Classify it as **automatable**, **assisted**, **blocked**, or **unknown**. Choose the
first subscription adapter using those results. Preserve limitations in its
capabilities. If none qualifies, continue useful YouTube programming and present
the smallest concrete setup/hardware tradeoff before expanding scope.

See [service research](RESEARCH.md#streaming-control-comparison-2026-09-21).

## M5 — Add one service without rewriting the engine

Implement the selected playback adapter and its auth/config boundary. Add the
service's exact content references to a small local catalog. Reuse the queue,
evidence, persistence, commands and contract suite unchanged where possible.

Acceptance:

- Two titles and three separate start/status/stop attempts demonstrate the claimed
  capabilities. Full automatic queueing requires a verified end signal as well.
- Exercise YouTube → subscription service and the reverse direction. Old provider
  callbacks cannot change the new session or trigger a second launch.
- A missing capability produces a precise unsupported/attention-required outcome.
  An assisted adapter can ship, but does not pass unattended cross-service acceptance.
- Adding this second real adapter validates and, if necessary, simplifies M1's
  protocols. Do not preserve a generic abstraction just to avoid changing it.

## M6 — Cueby programs an evening

Begin with a curated catalog of roughly twenty known playable items, including
familiar favorites and adjacent discoveries. Cueby proposes a lineup from mood,
time budget, available services, exclusions and a comfort/discovery preference.
Keep explanations and availability evidence with each selection.

Use ten fixed planning scenarios to verify that unavailable services, excluded
content and invented provider IDs never enter an executable queue. Keep duration
as an estimate; define the session budget as a soft boundary between programs
unless the user explicitly requests a hard stop. User edits and skips feed an
explicit preference history. Three accepted real lineups establish usefulness.

The planner can use an LLM, but deterministic validation and the session runner own
execution. YouTube-only programming is a useful release while M4/M5 are unresolved.

## M7/M8 — Interface and dependable use

Chirp is a good candidate for a small server-rendered interface with Kida components
and live SSE updates. Show the lineup, selection reasons, current observations and
their age, and stop/pause/skip where supported. Views consume the same read model
as MCP; browser routes never own Cast connections. Reconnecting a page must not
start playback. If the interface is remotely accessible, define access controls
as part of that deployment rather than assuming a local interface is a hosted app.

For M8, record runtime/library versions, service/device capabilities and every
attempt across three evenings. Test startup, shutdown, interruption and recovery.
Document the requirement that the local controller host remain awake/reachable.
Claim only the tested services and configurations. Free-threaded Python is a
separate compatibility lane, not a prerequisite for the useful release.

## Immediate next work

Finish M1: domain models, adapter/store/clock boundaries, validation and replay
fixtures, preserving the successful playback behavior. Quality tooling and initial
JSON record annotations are in place. Then run M2a before writing
automatic advancement. M4's inventory and route research can proceed alongside
that work; it need not block the core.
