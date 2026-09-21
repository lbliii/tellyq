# Changelog

## Unreleased

- Interpret a finite set of source-qualified YouTube player states and attribute
  valid incremental terminals through ordered content history. Preserve a separate
  historical completion witness, invalidate uncertain chains, and keep routine
  status windows within the evidence freshness limit.
- Add a bounded single-video checkpoint runner with private command/observation
  journaling, isolated state, optional guarded pause/resume and owned-session
  cleanup. Unconfirmed terminal candidates cannot shorten its observation budget.
- Add opt-in YouTube metadata diagnostics with bounded field types/counts and a
  separate private schema capture. A reviewed numeric `customData.playerState`
  field was initially diagnostic only; the completion interpretation above now
  uses a separately reviewed finite contract.
- Record two scoped metadata runs and pinned first-party YouTube source evidence.
  They identify a provider-state candidate for completion work; ad-specific codes
  remain unobserved and automatic advancement stays disabled.
- Record the repaired live diagnostic on `e4b2a91`: 531 offline tests, verified
  pause/resume and cleanup stop, plus user-confirmed visible pause. Completion
  remains unverified. Document available duration, incremental Cast status,
  provider custom-data limitations and the bounded next metadata investigation.
- Record the merged M2 foundation checkpoint at `7a08929`: 487 offline tests and
  packaging checks pass; three live requested-title ending candidates remain
  unconfirmed, pause/resume fails acceptance, and subsequent content reuses a
  media-session ID. Document the independent repair batch and repeat-live gates.
- Document the merged continuous lifecycle capture/replay, guarded pause/resume
  commands and SQLite queue/journal foundation. These do not enable unattended
  advancement; SQLite is not yet integrated into the playback runner.
- Describe M2a control repair: reconcile strictly pre-boundary startup events
  using fresh ownership, and expose fixed stage/reason diagnostics for refusals,
  receiver responses and unknown outcomes. Start/stop receipt shapes are unchanged.
- Describe M2a completion diagnostics: preserve the shapes of named wire fields,
  add offline capture inspection, and retain a reviewed partial live replay fixture
  for different content reusing a media-session ID. Unknown ads remain unknown;
  the completion gate is unchanged.

M1's foundations and second-wave code are merged. Actual `main` at `71a3c39`
passed 351 tests, packaging/install checks, macOS/Linux CI and the bounded live
regression. [M1 is accepted at that commit](docs/M1-ACCEPTANCE.md); unknown-ad
limitations remain explicit, and natural endings/advancement belong to M2.

- Add immutable domain values, backend/store/clock protocols and pure evidence
  policy, validated version-1 queue and legacy session storage, and defensive
  Cast normalization with synthetic offline replay fixtures.
- Integrate one-program operations through backend/store/clock contracts, with
  an additive `schema_version: 1` report boundary and compatible CLI command names.
  Preserve existing queue/session formats; new versioned snapshots retain
  ownership/history without restoring old monotonic playback evidence.
- Separate observed player state from playback proof. Unknown ad state remains
  unknown and produces an unconfirmed strict playback result even when the
  receiver reports playing. User confirmation stays separate from receiver proof.
- Address stop verification for a fresh correlated Chromecast idle reply that
  omits `applications`; reject malformed, partial and stale replies. Stop exits
  YouTube to the Chromecast home screen rather than pausing the video.
- Record the merged foundation checkpoint and its visible-stop/software-verification
  mismatch and its successful regression: visible playback, status without
  restarting, machine-verified app exit, user-confirmed Chromecast home screen
  and persisted `stopped` state.

### Original playback proof and repository setup

- Add the Python 3.14 Chromecast experiment: discover, queue, start, status, stop,
  and direct probe with explicit playback evidence and session ownership checks.
- Record the verified YouTube milestone, architecture direction and measurable
  roadmap for reliable queues, Milo MCP, additional services and optional Chirp UI.
- Add typed JSON records, Ruff, ty, pytest, coverage reports, uv locking, optional
  commit hooks, macOS/Linux CI and source/wheel installation checks.
- Store installed CLI state in the current working directory's `runtime/`, reject
  non-object state files and treat missing sample timestamps as unconfirmed.
