# M2a checkpoint and repair acceptance

Updated 2026-09-21. **M2a's lifecycle-signal gate passed on reviewed combined
candidate `2599345`, based on merged `fe73b6d`: three supported natural endings,
two titles and one verified pause/resume.** Controls previously passed on `e4b2a91`. Earlier failed and inconclusive attempts remain below; the new
completion batch and its exact validation are recorded at the end. Candidate
results do not claim that these changes are already on `main`. No automatic queue
advancement was attempted or enabled.

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

## First checkpoint live outcomes (`7a08929`)

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
had not yet been verified through an observed live pause/resume pair at this
checkpoint. The repaired run below subsequently passed that gate.

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

These are offline repair results. The historical A3 rejection remains unattributed.
New wire-field shapes do not establish completion support by themselves.

## Repaired live diagnostic (`e4b2a91`)

The repair PRs merged, and actual `main` at `e4b2a91` passed **531 offline tests**.
One requested diagnostic then launched title A from an idle receiver, after the
passive observer signaled readiness. A persistent controller kept the launch,
pause, resume and fresh status observations on one connection. Existing local
queue/session files remained unchanged.

| Gate | Observed result |
| --- | --- |
| Guarded pause | ACCEPTED with receiver/media-status provenance, followed by fresh owned PAUSED and `control_observed=true` |
| Guarded resume | After a 20-second pause hold, ACCEPTED followed by fresh owned PLAYING and `control_observed=true` |
| Progress after resume | Same-owned status advanced from 17.405 to 23.5 seconds |
| Visible pause | User confirmed: “Yes, I saw it pause.” |
| Natural terminal | One FINISHED candidate; terminal content and ad state remained unknown |
| Cleanup | Guarded stop accepted and receiver app exit verified through telemetry |
| Capture integrity | Normal 240-second budget ending, end record present, no partial windows; sanitize/replay/inspect passed |

Visible resume, natural completion and cleanup were not separately confirmed. The
strict evidence reason stayed `ad_unknown`; verified controls do not manufacture
inactive-ad evidence. No capture/controller tool session remained running.

The 119 media observations showed object-shaped status custom data throughout,
including the terminal. Primary media custom data was object-shaped in the 109
exact-title playback observations and unavailable in ten terminal/empty ones.
Terminal primary media, extended status, break status and queue-item IDs were
absent. Shapes cannot tell us whether the custom data was empty or meaningful.

Duration was already present during playback: **112.901 or 113.0 seconds** for
the requested NASA title. The last exact-title position was 112.777 seconds; the
terminal supplied neither duration nor identity. These are diagnostic context,
not a time-based completion decision. The [metadata investigation](YOUTUBE-METADATA.md)
separates documented semantics, library caching and observed fields.

## Metadata diagnostic and acceptance plan at `fe73b6d`

The repaired diagnostic narrowed the next step to provider custom data. The first
scoped metadata run used observer `61b839e` with the unchanged `e4b2a91` controller.
Its 200-second capture ended normally with 90 media samples, zero dropped samples
and no structural truncation. Of those, 83 identified the requested title and seven
omitted identity; none identified other content. One FINISHED candidate omitted
identity and duration, retained position 112.901, and matched the immediately
preceding media-session ID. Ad state remained unknown. No visual confirmation was
requested for this run. Guarded cleanup was accepted and verified `stop_observed`;
legacy queue/session hashes remained unchanged.

All 90 status custom-data objects contained one numeric `playerState` field. The
83 media custom-data objects contained numeric `currentIndex` and string `listId`;
the remaining seven were unavailable. No other keys appeared. The schema capture
saved names/types only, so it cannot retrospectively supply their values. The
reviewed field names are protocol vocabulary; playlist identifiers remain private.

YouTube's own public remote-player implementation reads the exact custom-state
path and distinguishes ad-specific numeric states. This justifies a narrow numeric
diagnostic, not a production ad mapping. The [research note](YOUTUBE-METADATA.md#a-concrete-provider-state-lead)
pins the source build and separates direct source behavior from inference.

The follow-up observer at `3a60058` retained the selected numeric field while the
controller stayed on `e4b2a91`. It observed -1 during startup, 3 with BUFFERING,
1 with PLAYING, and **0 in the anonymous IDLE / FINISHED message**. The next sample
reported the requested title at position zero with BUFFERING and code 5, using
the same media-session ID. Code 5 remains uninterpreted. No 108x ad-specific code
was observed; ad behavior is therefore still unvalidated. These findings identify
a concrete source-backed candidate for further interpretation, not M2a acceptance.

The numeric capture ended normally at its 200-second budget: **93 samples**, zero
drops, no structural truncation, 87 requested-title and six anonymous observations,
and no other content. Code counts were -1: 4, 3: 4, 1: 59, 0: 1 and 5: 25. Every
selected numeric field was valid; all 93 normalized ad states remained unknown.
There was no new visual confirmation. Existing queue/session hashes were unchanged.
Guarded cleanup accepted stop and verified `stop_observed` after fresh BUFFERING.
Both runs' capture, launch and cleanup tool processes exited normally.

Diagnostic code `3a60058` ([PR #18](https://github.com/lbliii/tellyq/pull/18)) passed
**566 tests**, **93.3% coverage**, lint, formatting, ty, source/wheel builds,
isolated installation and macOS/Linux CI. These checks validate diagnostic
implementation, not a new completion capability. This batch consisted of two
scoped live runs: schema discovery followed by a targeted numeric probe.

Evaluate ordered content history under Cast's incremental-update contract; the
media-session ID alone is insufficient. A source-qualified ad rule could be
legitimate without a literal false boolean, but the existing gate is unchanged
in this batch.

The next batch was planned to review a finite YouTube-specific state interpretation and
ordered content attribution using the pinned source and observations. It needs
offline cases for unknown/invalid codes, ads, conflicting fields, reused IDs,
replacement, gaps and reconnects before a policy change and full live acceptance.
Schema-only capture has answered its question; do not repeat it. The decision and
bounded fallback are in the [metadata investigation](YOUTUBE-METADATA.md#decision-and-bounded-next-work).

The acceptance plan for a supported evidence route was:

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

## Completion candidate validation

The metadata PRs merged at `fe73b6d`, which passed 566 offline tests. Two independent
implementation branches target that baseline:

| Stream | Candidate | Independent validation |
| --- | --- | --- |
| [Completion core, PR #20](https://github.com/lbliii/tellyq/pull/20) | Code `33b3261`; subsequent changes only update documentation | 635 tests, Ruff/format/ty, source/wheel builds and isolated installation |
| [Single-video checkpoint runner, PR #21](https://github.com/lbliii/tellyq/pull/21) | `ecdee48` | 597 tests, including 31 focused runner cases, plus lint/types/build/install |
| Exact combined code | Temporary integration commit `259934502435276f2d6ecbb63b501c5b94ea940f` | 666 tests, 93.8% branch-inclusive coverage, Ruff/format/ty, builds and isolated installation |

Both code merge orders produce tree `d276060b7ad780377a4a5c013f2322e8ea0ede17`.
The later core documentation change records observed pause evidence and does not
change the tested implementation. The [completion plan](M2A-COMPLETION-PLAN.md)
records ownership and boundaries; each PR remains independently testable.

Code review qualified provider codes against the pinned first-party source, kept the
provider interpreter outside generic policy, and covered unknown/malformed fields,
positive ad context, conflicting states, stale samples, sequence gaps, replay
partials, reconnects, source changes and replacement. It also fixed same-batch
completion followed by replacement, and routine status windows that were longer
than the existing evidence freshness limit. The runner starts its final collection
window only after a verified completion; an unconfirmed or ad terminal cannot
shorten the run. Verified pause ownership must survive until resume dispatch.

### Completion rule exercised live

The finite YouTube interpreter requires fresh exact receiver-app identity,
consistent standard/player states and the reviewed provider code. Absent break
metadata alone does not prove inactive ads. Qualified ordinary code 0 with
IDLE / FINISHED may be attributed to the requested content through recent,
contiguous, same-source ownership/progress history. The raw terminal keeps its
missing content identity. The report records `ordered_history` attribution,
source, terminal sequence and the exact-content anchor separately.

One historical witness survives later buffering and cleanup. It cannot grant
ownership over a replacement session or create a second completion. Neither
published duration, elapsed time, reaching duration nor app exit is used to infer
an ending. A future queue runner must consume the witness once and reconcile
current ownership before launching anything else.

### Three-run live acceptance (`2599345`)

All three invocations used the same unchanged tested checkout, CPython 3.14.0
with the GIL enabled, PyChromecast 14.0.10 and casttube 0.2.1. Each required a
fresh idle receiver before launch, ran one selected video, and used an isolated
session store while retaining the private legacy queue/session hashes. The
observation budget was 240 seconds with a 20-second window after confirmed
completion. A1 included a 20-second guarded pause hold with continuous observation.

| Attempt | Natural-completion evidence | Controls and cleanup | Process result |
| --- | --- | --- | --- |
| A1: title A | One anonymous FINISHED/code-0 terminal, attributed through ordered history; one witness | Pause and resume observed; subsequent stop observed; final state STOPPED | Exit 0, normal post-terminal window |
| B: title B | Two FINISHED candidates, first anonymous and second explicit; one completion witness | Stop observed; final state STOPPED | Exit 0, normal post-terminal window |
| A2: title A repeat | One anonymous FINISHED/code-0 terminal; one witness retained after replacement in the same observation batch | Cleanup refused after known replacement; no stop command dispatched | Exit 1 signals refused cleanup, not a failed completion or an exception |

All three reported `receiver_playback_observed=true` and
`natural_completion_observed=true`. There were no observation/backend errors and
all journals contain normal end records. Legacy queue/session hashes remained
unchanged. A1 and B preserved ENDED through later code-5 BUFFERING until the
separate cleanup stop. A2 ended with current state UNKNOWN / `session_replaced`
and `ownership_lost=true`, while retaining its historical completion witness.
The runner left replacement playback untouched and all controller processes exited.

The user confirmed seeing A1 pause and resume. They did not observe the first
ending when initially asked, then later confirmed that the last clip they watched
ended normally. That later reply is retained as visual evidence without assigning
it to a particular attempt. After A2, the user separately confirmed seeing an
ad or another video. Cleanup and each individual ending were not visually confirmed. Software and visual evidence therefore remain distinct.

Provider-code counts across the complete private journals were:

| Run | Media samples | Code counts |
| --- | --- | --- |
| A1 | 99 | -1: 4; 3: 7; 1: 60; 2: 16; 0: 1; 5: 11 |
| B | 64 | -1: 5; 3: 3; 1: 43; 0: 2; 5: 11 |
| A2 | 96 | -1: 16; 3: 8; 1: 65; 0: 1; 5: 1; 1081: 5 |

A2 supplied the first positive live ad-family samples: five code-1081 messages
after the requested program ended and different content appeared. The standard
Cast state was BUFFERING; the adapter correctly retained that state while
reporting `ad_active=true` and provider phase AD. The same historical completion
remained unchanged. Different content subsequently reached PLAYING. This is
consistent with provider autoplay, but the cause of the transition is not proven.
No second TellyQ launch occurred in this invocation.

All observation windows were projected from each private checkpoint journal into
the lifecycle format, preserving raw normalized samples, order and window return
times. Sanitization and offline replay reconfirmed each completion. These are
projections of the existing runs, not new passive captures or new hardware tests;
command chronology remains in the source journals. The reviewed projections and
full logs stay in ignored `runtime/`.

### Acceptance and remaining limits

The lifecycle-signal gate passes for this exact candidate and observed receiver.
The three endings and verified pause/resume support starting M2b implementation
after these PRs merge and the merged checks pass. The nonzero A2 cleanup result is
retained explicitly: refusal after replacement is the intended ownership behavior.
Earlier failed/inconclusive attempts remain in this ledger; these three new runs
do not erase them or establish statistical reliability.

Live evidence covers code 1081 after completion, not every ad-family code,
mid-program ads, ad endings, ad pause/resume or disconnect/recovery. Those paths
have conservative synthetic coverage and remain targets for later live testing.
Unknown codes -1 and 5 still have unknown semantics. The provider contract is
pinned implementation evidence, not a stable public YouTube API guarantee.

Natural completion is process-local history. A restart cannot restore monotonic
freshness or use an old witness to launch another item. M2b must integrate durable
intent and recovery, consume one completion once, cancel pending work on stop,
and hold on provider replacement rather than competing with it. No persistent
queue runner, automatic handoff or unattended queue acceptance has been delivered.
