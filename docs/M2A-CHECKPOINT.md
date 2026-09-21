# M2a checkpoint and repair acceptance

Updated 2026-09-21. **M2a remains blocked after the live checkpoint on `7a08929`.**
The first M2 batch is merged. Its offline checks passed, but the live session did
not verify pause/resume or establish a sufficient completion signal. No automatic
queue advancement was attempted or enabled.

## Tested baseline

Merged `main` at `7a08929` passed `poe check` and `poe ci` on standard CPython
3.14: **487 tests**, **92.8% branch-inclusive coverage**, Ruff, formatting, ty,
source/wheel builds and isolated installation smoke checks. These are the results
of the checkpoint baseline, not validation of the subsequent repair branches.

The supervised session used two official NASA YouTube titles:

- A: [Why Does NASA Study Earth?](https://www.youtube.com/watch?v=f9F7yDjSdNA)
- B: [Dishing the Dirt](https://www.youtube.com/watch?v=hgsIFyITvJE)

Each passive observer started before its launch. A separate private controller
used the application API and an isolated session store, leaving the existing
one-item queue/session unchanged. No seek or duration-based completion decision
was used. User confirmation of visible playback was not received for these runs.
Device identity, addresses, raw traces and command records remain in ignored
`runtime/`; this document records only reviewed outcomes.

## Live outcomes

| Run | Requested-title ending candidate | Pause/resume evidence | Capture limitation |
| --- | --- | --- | --- |
| A1 | `IDLE / FINISHED`, following exact-title PLAYING | Two new-client pause attempts refused; no PAUSED observation | First failure record did not retain its exception/baseline details |
| B | `IDLE / FINISHED`, following exact-title PLAYING | Persistent controller refused pause while its fresh state was BUFFERING; no effect or resume | Capture was interrupted after the terminal observation; it is incomplete |
| A3 | `IDLE / FINISHED`, following exact-title PLAYING | Fresh PLAYING and advertised pause support preceded one attempt; final receipt REJECTED, `control_observed=false`; no hold or resume | Rejection record cannot distinguish a local guard from a receiver reply |

All three first terminal candidates omitted content identity and reported unknown
ad state. Each matched the preceding requested-title media-session identifier.
That correlation is useful diagnostic evidence, but it is insufficient for strict
completion. The passive observer cannot establish absence of external control.

A3 subsequently recorded **different content in BUFFERING and PLAYING states with
the same media-session identifier**, then another FINISHED event. This is
consistent with provider autoplay, but the cause was not established. The extra
event is not another requested-title run. Session identity alone cannot attribute
an ending; the content history and observation boundaries also matter.

The pause journal's initial UNKNOWN outcome records intent before dispatch. It is
not the final outcome: A3 ended with REJECTED. Receiver-advertised pause support
has not been verified through an observed live pause/resume pair.

An offline reproduction also found that a queued startup reset from before the
current control boundary rejects an otherwise valid pause. An identical synthetic
case without the reset succeeds. This proves a control-boundary defect, but does
not prove it caused A1's live refusal: that control client's detailed baseline
was not retained, and the observer's separate connection is not a substitute.

Sanitize/replay commands succeeded for all three captures. This alone does not
approve their contents as public fixtures or turn partial traces into acceptance.
All observer/experiment processes exited. Guarded cleanup refused after ownership
was no longer safe; replacement playback was left untouched.

## Repair batch

Both code streams start independently from `7a08929` and target `main`. Neither
depends on the other branch, and neither performs live hardware operations.

| Stream | Owned work | Offline acceptance |
| --- | --- | --- |
| [Controls PR #15](https://github.com/lbliii/tellyq/pull/15) | Application control boundaries, Cast control adapter/transport, typed rejection provenance, CLI reports and control tests | Pre-boundary startup events allow fresh ownership reconciliation; current resets/takeover still refuse. Fixed safe reasons distinguish application/adapter/transport guards, receiver rejection and unknown outcomes. No private payloads leak into public diagnostics. |
| [Completion evidence PR #16](https://github.com/lbliii/tellyq/pull/16) | Lifecycle capture/replay, relevant wire normalization, reviewed fixtures and completion diagnostics | Missing fields remain unknown; later content sharing a media ID cannot complete the old item. Preserve diagnostic provenance needed to determine whether metadata was absent or discarded. Document a justified signal or the precise remaining blocker. |
| Coordination | Checkpoint ledger, roadmap, cross-review and exact combined validation | Both branches pass independently; combined code passes `poe ci`; final code heads pass macOS/Linux CI. |

The coordinator reviews shared model/adapter additions and tests both merge orders
before publishing PRs. The implementation does not introduce a timer substitute,
relax unknown ads into inactive ads, or enable the M2b runner.

## Repair validation

| Candidate | Local validation |
| --- | --- |
| Controls `0f959b3`, PR #15 | Locked Python 3.14 `poe check`: 516 tests, Ruff, formatting and ty |
| Completion `9071eb0`, PR #16 | 503 tests, Ruff, formatting and ty; packaging/installation preflight passed before the final test-only assertion repair |
| Exact combined tree at temporary commit `85e9048` | `poe ci`: 531 tests, 92.9% branch-inclusive coverage, Ruff/format/ty, source/wheel builds, fixture-membership validation and isolated installation/CLI smoke |

Final code heads passed Python 3.14 macOS and Linux CI:
[controls](https://github.com/lbliii/tellyq/actions/runs/35631078588) and
[completion diagnostics](https://github.com/lbliii/tellyq/actions/runs/35631078357).

Both merge orders produce the same tree, `f9d40a7c267da32a3e17e65db51ae85e595f6bc9`.
Cross-review checked the actual control transport path, refusal provenance,
ownership rules and source-to-excerpt redaction. Review also found and fixed an
omitted lifecycle fixture in the source archive; packaging smoke now checks all
reviewed fixture formats. Platform CI exposed an older flaky privacy assertion
that mistook year digits in a random pseudonym for leaked timestamps. Both branches
now check timestamp fields and relative offsets directly, with a deterministic
regression for a harmless pseudonym containing those digits.

These are offline repair results. The historical A3 rejection remains unattributed,
and the repaired controls have not yet passed live pause/resume. New wire-field
shapes do not establish completion support by themselves.

## Next live checkpoint

After repair PRs merge, first run one explicitly requested short diagnostic with
the new wire-field shapes and control provenance. Use a fresh exact-title launch,
qualified pause/resume attempt, natural terminal observation and subsequent-content
window. Inspect the trace before scheduling more identical runs. If no supported
source supplies sufficient content/ad evidence, record the current route's
limitation and stop the completion experiment; keep M2a blocked. Shape availability
alone does not establish field semantics. See the bounded diagnostic in the
[lifecycle notes](M2-LIFECYCLE.md).

Once a supported evidence route exists, the full acceptance set must:

1. Pull the actual merged commit and run the automated checks. Select the existing
   approved receiver, inspect current playback, and preserve local state. Record
   code, interpreter/library versions and each launch boundary.
2. Start a bounded observer before each of three launches across at least two
   titles. Keep the control connection alive for the deliberate pause/resume run.
   Wait for fresh exact-title PLAYING and current support before attempting pause;
   retain every refusal and the stage/reason it supplies.
3. Verify a fresh owned PAUSED observation after an accepted pause, then resume
   and verify fresh owned PLAYING/progress. Record the hold separately. Neither a
   pause nor its duration consumes an item; only resume a verified owned pause.
4. Observe each real ending and a bounded subsequent-content window. Collect the
   new diagnostic fields, ads/unknowns, buffering, interruptions, identity changes
   and external actions. Obtain separate user confirmation of visible outcomes.
   Seeking, reaching duration, an accepted command and app exit do not count.
5. Finish captures normally when possible, preserve interruption markers when not,
   sanitize/review replay fixtures, and clean up only still-owned playback. Report
   failures and limitations without resetting the attempt count.

M2a passes only with three supported natural endings, at least two titles, a
verified pause/resume run and a justified completion decision. If the necessary
signal is still absent, record that specific blocker and hold advancement. M2b's
persistent owner, durable queue integration and automatic handoff tests remain
subsequent work in [the execution plan](M2-PLAN.md).
