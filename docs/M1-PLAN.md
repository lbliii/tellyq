# M1 execution plan: maintainable playback core

Baseline: `main` at `2f0e99b`, 2026-09-21. The first YouTube playback milestone
passed. Repository tooling is in place; its macOS and Linux CI jobs passed.
The current suite has 23 tests. M1 finishes the core boundaries before M2 adds
continuous monitoring and automatic queue advancement.

## Delivery strategy

Run three independent workstreams in isolated Git worktrees. Each branches from
the same `main` commit, passes checks against that baseline, and opens its own PR
with **base `main`**. No stacked PRs, cross-branch imports or cherry-picking another
agent's unfinished work. Each agent owns its listed files and adds its own tests.

The coordinator owns this plan and shared project configuration. Do not change
dependencies, move the entire package into `src/`, or edit shared documentation
in parallel. Source-layout migration can follow functional boundaries later.
Use stream-specific design notes so documentation changes also remain independent.

Separate branches avoid accidental shared edits; they do not eliminate semantic
integration work. The first wave deliberately ships components that are useful
and testable on today's `main`. A second wave wires the new contracts into the
controller only after the component PRs have been reviewed and integrated. Every
second-wave PR also starts from then-current `main` and targets `main`.

## Wave 1: parallel foundations

| Stream | Branch / ownership | Tasks | Acceptance |
| --- | --- | --- | --- |
| A: Domain contracts and evidence policy | `codex/m1-domain`; new `tellyq/domain/`, `tests/domain/`, `docs/m1/domain.md` | A1 frozen/slotted content, target, request, receipt, observation, capability, error and snapshot values; A2 minimal backend/store/clock protocols with test doubles; A3 pure evidence transitions; A4 deterministic replay and dependency-isolation tests | Pure core imports without Cast/Milo/Chirp installed; no clocks, sockets or disk inside policy; accepted commands cannot manufacture playback; stale, duplicate, reordered or replaced-session events cannot confirm progress/completion |
| B: Persistence and boundary validation | `codex/m1-storage`; `tellyq/state.py`, new `tellyq/clock.py`, `tellyq/programs.py`, `tests/test_state.py`, `docs/m1/storage.md` | B1 validate every required field of existing queue/session JSON; B2 explicit version/compatibility and useful errors; B3 preserve atomic writes and exclusive process ownership; B4 remove Cast imports from persistence and extract the default program | Existing version-1 queues remain readable; malformed and future-version records fail before device I/O; interrupted writes preserve old data and remove temporary files; stored state stays private; persistence imports without PyChromecast |
| C: Cast normalization and replay fixtures | `codex/m1-cast`; `tellyq/cast.py`, `tellyq/models.py` only for observation typing, new `tellyq/cast_messages.py`, `tests/test_cast_messages.py`, `tests/fixtures/cast/`, `docs/m1/cast.md`, and `MANIFEST.in` for fixture inclusion only | C1 parse unknown wire messages defensively; C2 preserve absent/invalid fields as unknown, including ad state; C3 decouple normalization from PyChromecast and callback ownership; C4 sanitized synthetic replay fixtures and callback tests | Malformed messages cannot crash the observer; bools/non-finite numbers do not become valid positions; empty statuses cannot reuse old metadata; callbacks transfer detached snapshots; parser imports without PyChromecast; fixtures are labeled synthetic and ship in the source archive |

All three streams preserve the current CLI vocabulary and existing tests. New
core values do not replace the current wire dictionaries in this wave. Storage
keeps the public `read/save/make_queue/load_queue/command_lock` entry points.
Cast keeps `media_observation`, `Observer`, `Connection`, `connect` and existing
exports compatible, apart from correcting invented values to explicit unknowns.

### Shared boundary decisions

- Content IDs are opaque and provider-specific. The domain does not parse YouTube
  URLs or contain the Bob Ross ID. Cast retains its current example constant until
  the integration PR can consume the extracted default program.
- An observation belongs to a target, content/session and connection generation;
  sequence and monotonic time order observations only within that generation.
  UTC timestamps describe records. A `Clock` supplies `utcnow() -> datetime` and
  `monotonic() -> float`; policy receives time as input.
- Represent missing ad information as unknown. The Cast parser does not decide
  playback success. Policy distinguishes command acceptance, observed playback,
  natural completion, stop and historical visual confirmation.
- Preserve version-1 queue compatibility. Reject unsupported future schema
  versions explicitly. Never silently overwrite or pretend to migrate unknown
  formats. A changed on-disk shape requires a documented migration in wave 2.
- JSON boundaries accept unknown input and validate before returning typed values.
  No blanket type ignores, unchecked TypedDict casts, fake success defaults, raw
  exception messages containing wire payloads, or unexplained swallowed errors.
- Test fixtures contain synthetic identities and protocol data only. Agents do
  not read private runtime reports or send discovery/playback commands.

## Wave 2: integrate after the foundations land

| Task | Work / owner | Exit evidence |
| --- | --- | --- |
| D1 | Coordinator: review each wave-1 PR, then test their combined changes in an integration worktree | Independent PR checks pass; disjoint ownership verified; full suite and package smoke pass together |
| D2 | Application agent: adapt YouTube/Cast to `PlaybackBackend`, adapt validated JSON to the store contract, inject clock/store/backend into controller operations | Same application flow runs with a fake backend and the Cast adapter; core/storage imports contain no Cast dependency; default episode comes from configuration/example data |
| D3 | Application agent: route fresh correlated observations through the pure policy, version command output and preserve CLI compatibility | CLI JSON contract tests; version-1 state migration/compatibility test; no duplicate start, false completion or stopping a replacement session |
| D4 | Verification agent, after D2/D3 land: reusable backend contract and end-to-end offline replay suite | Identical contracts exercised by fake backend and Cast with mocked transport; partial/stale/duplicate/out-of-order/takeover/timeout/ad scenarios covered; imported core tested with optional frameworks unavailable |
| E1 | Coordinator: final docs and M1 acceptance review | `uv run --locked poe ci` passes on macOS/Linux, package installation passes, roadmap records actual results and remaining hardware limitations |

D2 and D3 share controller ownership and therefore form one integration PR rather
than concurrent edits. D4 branches from `main` after that PR lands; it is not
stacked against an unmerged branch. No stream marks M1 complete merely because
its own component passes.

## Definition of done and review

Each PR describes the problem, behavior, validation and remaining limits. Run
`uv run --locked poe check`; run preflight for package/dependency changes. Keep the
baseline regressions or equivalent assertions. Re-run the combined suite after
integration; passing separate branch suites does not establish compatibility.

The completed milestone requires all of the roadmap's M1 acceptance criteria,
including versioned CLI output, real/fake adapter contracts and no dependency on
Cast in the core. A synthetic replay is not a new hardware result. Live lifecycle
verification remains M2a, including natural endings, ads, pauses and buffering.
No daemon, automatic advancement, MCP server, subscription-service experiment,
UI, new dependency or free-threaded support claim is part of this first wave.

## Progress ledger

- [x] Identify the remaining M1 work and confirm the baseline CI result.
- [x] Prepare independent worktrees from the same `main` commit.
- [ ] A: domain contracts and policy PR reviewed and integrated.
- [ ] B: persistence/validation PR reviewed and integrated.
- [ ] C: Cast normalization PR reviewed and integrated.
- [ ] D1: combined foundation checks pass.
- [ ] D2/D3: application integration and compatible versioned output land.
- [ ] D4: adapter contract and end-to-end replay gates pass.
- [ ] E1: M1 accepted; then schedule M2a hardware verification.
