# Changelog

## Unreleased

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
