# Playback implementation history

## Scope and order

1. Set up an isolated Python 3.14 environment. Try the free-threaded build if
   dependencies support it; ordinary Python threads are an acceptable fallback.
2. Install the released PyChromecast package and record dependency versions.
3. Discover Cast devices without launching an app. Select the intended living-room
   receiver by stable UUID, never by list position. Ask if its identity is ambiguous.
4. Send the official Bob Ross `FozIp7Va7dY` video ID using YouTubeController.
   Capture receiver events with timestamps, retaining missing values as unknown.
5. Verify title/content identity and advancing playback where available. If Cast
   telemetry is incomplete, record the user's visual confirmation separately.
6. Send stop, and verify its effect. Save the request, observations, errors and
   timing under ignored `runtime/`. An accepted command is not playback evidence.
7. After the connection is proven, add a persistent one-item queue and JSON CLI
   commands: `discover`, `queue`, `start`, `status`, `stop`.
8. Test evidence classification, target selection and state persistence with fake
   receivers; document the actual hardware outcome and reproducible commands.

## Threading

PyChromecast's socket worker and discovery services already use background
threads. Keep control commands serialized and hand observations back through
explicit synchronization. Report whether the process actually keeps the GIL
disabled after importing dependencies; do not force unsupported native extensions
into free-threaded execution.

## Boundaries

No graphical UI, MCP wrapper, recommendations, cross-service control or automatic
queue advancement in this milestone. No guessed completion from elapsed runtime.
Keep device addresses, UUIDs, pairing data and run reports in `runtime/`, out of Git.

## Acceptance evidence

- The intended program is visibly playing on the intended TV.
- Fresh observations show playback progress where the receiver provides it.
- Software stop takes effect, with the source of verification recorded.
- Buffering, ads, missing data and app launch alone do not count as success.

## Outcome: M0 playback proof passed, 2026-09-21

- Python 3.14.0 works with released PyChromecast 14.0.10 and its locked dependencies.
  The installed stable interpreter uses the GIL. Existing free-threaded shortcuts
  pointed at removed temporary installations, so the successful implementation uses
  ordinary background threads and `queue.Queue` for receiver observations.
- Discovery found three Cast devices. The user explicitly selected the Chromecast
  named “Projector.” Its UUID is saved only in local runtime state.
- The unmodified YouTubeController started `FozIp7Va7dY`. No compatibility patch was
  needed. The receiver supplied the exact title, PLAYING state and advancing time.
- The first receiver-only run happened while HDMI was disconnected and the TV was
  off. The user clarified that the Chromecast had been unplugged while testing the
  Sonos Beam. This run did not satisfy visible-playback acceptance.
- After the user reconnected Chromecast HDMI, a new start through the saved queue
  showed the requested episode progressing from approximately 1.7 to 23 seconds.
  The user confirmed “Bob Ross is visible; TV switched automatically.” This is
  separate human evidence, stored with the successful start report.
- A fresh status connection observed continued progress without a playback command.
- The final stop issued `quit_app` for the saved YouTube session. Subsequent receiver
  events showed no running app, confirming session exit. The queue was `stopped`
  at the end of that run; this is not a statement about current local state.
- Sixteen automated tests passed at that checkpoint. They cover target identity, uncertain commands,
  missing observations, false completion, repeated start/status behavior, session
  ownership for stop, atomic state writes and overlapping command protection.

## Delivered work

- `tellyq/cast.py`: device discovery, exact UUID selection, bounded connections and
  passive timestamped receiver/media observers. Thread communication uses a queue.
- `tellyq/evidence.py`: exact YouTube identity and observed playback progress.
- `tellyq/controller.py`: reusable discovery, queue, start, probe, status and stop.
- `tellyq/state.py`: private atomic JSON files and a process lock under `runtime/`.
- `tellyq/__main__.py`: JSON CLI; `pyproject.toml` also defines a `tellyq` entry point.
- `.python-version`, `uv.lock`, tests and reproducible README commands.

## Repository baseline

The follow-up setup adds typed JSON records and annotated package functions,
Ruff/ty/pytest, branch coverage reports, a locked build backend, optional prek hooks,
and macOS/Linux CI with source/wheel installation checks. All normal tests block
networking. The install smoke verifies imports and CLI help have no runtime side
effects and creates only a synthetic local queue. Installed commands now place
`runtime/` under the working directory. See [CONTRIBUTING.md](../CONTRIBUTING.md).

M1's foundation PRs have since merged: immutable domain values and ports, pure
evidence rules, validated atomic JSON storage and defensive Cast normalization.
The persistent session runner belongs to M2. No live hardware session is repeated
as part of repository setup.

## M1 foundation checkpoint, 2026-09-21

The merged `main` checkpoint at `a2b72a9` passed 251 tests with 91.2% branch-inclusive
coverage on standard Python 3.14.0. Ruff, formatting, ty 0.0.82, source/wheel builds
and isolated installation checks passed. These results establish the component
baseline; they do not establish that the controller uses every new contract.

A separately requested live regression observed the exact Bob Ross content ID
and advancing playback. The user confirmed visible playback; two status checks
did not restart it. `quit_app` returned, and the user confirmed that the TV returned
to the Chromecast home screen. That is the intended stop behavior, not a pause.
However, the parser rejected idle receiver replies lacking `applications`; the
report said `unconfirmed` and the queue retained `playing`. The visible stop
succeeded, while software stop verification and persistence failed this checkpoint.

Application integration, corrected stop evidence and a shared fake/Cast contract
suite are the second M1 wave. [Stop-evidence PR #8](https://github.com/lbliii/tellyq/pull/8)
at `a0f6219` and [application PR #9](https://github.com/lbliii/tellyq/pull/9) at
`92bea46` each passed independent checks. Their exact combined candidate passed
351 tests with 91.8% branch-inclusive coverage, Ruff/format/ty, source/wheel builds
and isolated install/CLI smoke. These are offline results; no new hardware run,
natural ending or automatic advancement was tested.

PR #8 merged at `110cf52`, followed by PR #9 at `71a3c39`. The coordinator pulled
actual `main` at `71a3c39` and reran `poe ci`: 351 tests, 91.8% branch-inclusive
coverage and all lint/format/type/build/install checks passed. The merged-main
[macOS/Linux CI run](https://github.com/lbliii/tellyq/actions/runs/35620756804)
also passed.

The coordinator then ran the explicitly requested hardware regression on that
commit. Projector was initially idle; the user freshly confirmed visible Bob Ross.
Exact content/title and PLAYING telemetry advanced from 23.434 seconds after start
to 57.259 and 96.695 across two status reads in the same app/media session, without
a restart. Ad state remained unknown, so strict playback proof remained
unconfirmed; the raw progress and visual confirmation stayed separate evidence.
The stop report confirmed receiver-app exit, the user confirmed the Chromecast
home screen, and the queue persisted `stopped`. A new store instance restored stop
intent with unknown current playback state, not old evidence. **M1 passed at
`71a3c39`.** The [acceptance record](M1-ACCEPTANCE.md) preserves the detailed gates
and remaining limitations.

## Material limits and next work

M0 and M1 are complete. Full-episode completion, ads/buffering over
an entire program and advancing to a second program have not been tested. The
[roadmap](ROADMAP.md) finishes the typed-core milestone before validating those
observations and implementing automatic queue advancement. A Milo MCP interface
can then expose the same application commands.

There is no standalone Samsung/Sonos power-control integration. During diagnosis,
the only Samsung service found was a desk monitor, so no power command was sent to
it. The successful retry relied on the connected Chromecast and the TV's existing
automatic switching behavior. A separate power-on/off API and subscription-service
playback remain future work.

All detailed observations and device/session data remain in ignored `runtime/`.
Published runtime is never treated as evidence of completion.
