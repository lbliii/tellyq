# Inspecting automatic handoff holds

The M2 closing diagnostics expose the existing automatic release and successor
checks. **They do not relax the handoff policy or claim a live handoff fix.** The
live M2b gate remains open. No discovery, playback, or receiver control was
performed for this change. A fresh supervised checkpoint must identify the exact
current hold before any evidence-supported repair or acceptance claim.

## Cached status contract

`TaskView.handoff` is an immutable, optional diagnostic. `status` and command
tickets project it as additive `view.handoff` in the version-1 IPC response. Tasks
that supply no diagnostic emit null; the updated decoder also accepts older
version-1 views that omit it. Older clients whose decoder rejects unknown view
fields must be updated with the owner. No stored session or queue schema changes.

The projection contains:

- `stage`: `queue`, `release`, or `successor`.
- `disposition`: `hold`, `ready`, or `complete`. Ready means eligibility at that
  stage at observation time. It never means a release or successor was dispatched,
  accepted, observed, or visibly playing.
- `reason`: a finite `HandoffReason` value describing the current gate. A queue
  policy hold also includes the existing reducer's finite `queue_reason`.
- `evidence`: one fixed-size observation summary, or null when unavailable.
- `first_veto`: the first permanent release veto and its fixed-size evidence,
  or null. It survives later harmless callbacks, duplicate returned batches,
  replacement returning to old identity, delayed idle and cancellation. It is
  process-local and clears only with a new attempt, never by restoring saved state.

Evidence contains sequence, relative age/time/sequence deltas, terminal sequence
and position, observed player/idle/provider phase, position, ad state, identity
update and reset/session flags. Content, receiver session, app, media, target and
connection identities are represented only by match booleans; absent identities
remain null. No arbitrary provider source, exception text, addresses, tokens,
raw payload, wall-clock timestamp or raw monotonic clock is copied into the new
projection. The existing queue projection still contains the configured target.

`age_seconds` is measured when a diagnostic is evaluated. Reading cached status
or an old command ticket does not refresh it; use the owner's snapshot revision
and existing playback observation timestamp to assess how recently the view was
updated. First-veto tracking does not receive a current clock, so its age is null;
its terminal-relative time and observed inter-event deltas remain available.
A retained baseline-refusal explanation likewise describes the original refusal,
not newly granted authority after later identity changes.

Normalized `ProviderEvidence` has a phase but no numeric provider code. The
projection therefore does not invent code 5 from BUFFERING or a position reset.
Unknown provider phase and ad state remain unknown. The committed replay fixtures
retain the observed numeric field at the sanitized input boundary.

## Reading a hold

A settled first item followed by a same-content reset normally reports
`stage: release`, `reason: authority_blocked`, with
`first_veto.reason: position_rewound`. The veto evidence shows the terminal and
new positions, `content_matches: true`, unchanged scope comparisons, and unknown
ad/provider state. Later cancellation changes the current stage/reason to
`queue/canceled` while retaining that first veto.

A known takeover reports `ownership_lost`, including when the attempt remains
CURRENT. Historical completion remains separate. A different-content event after
completion retains `content_changed` as the first release veto even if an ad is
observed afterward. Current evidence and the original veto can describe different
events; the first veto is never rewritten into the latest event's explanation.

Other holds distinguish missing authority, stale/unqualified terminal,
sequence/observation gaps, clock discontinuity, changed connection/session/app/
media/target, positive ads, restarted PLAYING/PAUSED, missing or stale current
receiver/media identity, rejected or unresolved durable release, and waiting for
observed release idle or fresh successor idle. A refused baseline retains its
specific explanation; later telemetry cannot erase why the one permitted release
was rejected. Receipts and durable command state remain separate from receiver
observations.

## Evidence and remaining boundary

[The fixture ledger](../tests/fixtures/handoff/README.md) records selected,
contiguous ending windows from three previously completed live checkpoints.
Offline replay recomputes Cast normalization, ordered lifecycle completion and
release guards. It never trusts saved completion flags and cannot invoke playback.

| Historical timeline | Recomputed completion | First permanent release veto |
| --- | --- | --- |
| A1: anonymous FINISHED, same-content code 5 / BUFFERING / position zero | Retained | `position_rewound` |
| B: anonymous and explicit FINISHED, same-content code 5 / BUFFERING / position zero | Retained | `position_rewound` |
| A2: FINISHED, different content, later code 1081 ad | Retained with ownership lost | `content_changed` |

These excerpts are historical observations, not full-run or new live acceptance.
The current implementation does not establish the meaning of code 5. There is no
basis here to identify it as CUED, manufacture inactive-ad evidence, ignore its
position reset, extend freshness, or bypass takeover/generation checks.

Focused tests cover every existing release guard, sticky veto retention, current
and completed takeover, cancellation, retained preflight refusal, staged synthetic
handoffs, safe bounded IPC projection and immutable cached ages. Existing real
SQLite/application composition remains covered. The normal test network guard
continues to prohibit receiver/network access.
