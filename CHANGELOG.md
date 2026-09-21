# Changelog

## Unreleased

M1's foundation components are merged. The second-wave entries below describe
review work; final acceptance remains open in [the M1 record](docs/M1-ACCEPTANCE.md).

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
  mismatch, with explicit M1 acceptance gates and a bounded future hardware rerun.

### Original playback proof and repository setup

- Add the Python 3.14 Chromecast experiment: discover, queue, start, status, stop,
  and direct probe with explicit playback evidence and session ownership checks.
- Record the verified YouTube milestone, architecture direction and measurable
  roadmap for reliable queues, Milo MCP, additional services and optional Chirp UI.
- Add typed JSON records, Ruff, ty, pytest, coverage reports, uv locking, optional
  commit hooks, macOS/Linux CI and source/wheel installation checks.
- Store installed CLI state in the current working directory's `runtime/`, reject
  non-object state files and treat missing sample timestamps as unconfirmed.
