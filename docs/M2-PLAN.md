# M2 execution plan: observe endings, then run a queue

M2 is now closed with the known enqueue-stall limitation. This document preserves
the execution history; see the [aggregate closeout](M2-ACCEPTANCE.md) for delivered
capabilities, all four recent session attempts and the open follow-up defect.

Started 2026-09-21 from `main` at `8a480ea`, after the M1 acceptance record merged.
M1 passed on code commit `71a3c39`; its verified start/status/stop behavior is the
regression baseline. M2 adds a reliable unattended YouTube queue, in two stages.

The first batch is merged at `7a08929`. Its merged-main offline checks passed;
the supervised M2a checkpoint found terminal candidates but did not verify
pause/resume or reliable completion. Repairs merged at `e4b2a91`; 531 offline
tests and a live pause/resume/cleanup diagnostic passed. The user confirmed visible
pause. Two focused metadata runs then identified a provider-state candidate.
The completion batch implements its interpretation and ordered attribution, with
a repeatable single-video checkpoint runner. Its acceptance is recorded below.
See the [checkpoint and repair acceptance record](M2A-CHECKPOINT.md).

## First batch: prepare lifecycle experiments and durable state

Three agents work in separate worktrees, each branched from `8a480ea`. Each PR
targets `main` directly and must pass without another agent's branch. The
coordinator reviews shared contracts, combines the exact commits in a temporary
checkout, runs the full checks, and publishes a separate planning PR. Agents do
not control hardware or merge their PRs.

| Stream | Branch and ownership | Measurable deliverable |
| --- | --- | --- |
| L1: Continuous lifecycle capture | [PR #14](https://github.com/lbliii/tellyq/pull/14), `codex/m2-lifecycle`; new lifecycle module, capture script, focused tests and lifecycle notes; narrow additive adapter changes | A bounded read-only capture preserves normalized observations across a whole program, writes incremental private records, survives interruption, and exports explicitly labeled sanitized fixtures. Replay tests distinguish unknown, ads, buffering, pause, app exit, takeover and terminal evidence. |
| C1: Pause and resume | [PR #12](https://github.com/lbliii/tellyq/pull/12), `codex/m2-controls`; playback contracts, Cast normalization/controls, application/CLI and focused tests | Explicit commands require current ownership and advertised or verified support. Command acceptance and observed effect remain separate. Unknown support, stale identity and takeover refuse the effect. Existing start/status/stop behavior remains covered. |
| S1: Durable queue and command journal | [PR #13](https://github.com/lbliii/tellyq/pull/13), `codex/m2-storage`; new queue values/port, SQLite adapter, tests and storage notes | Atomic queue/attempt/intent transactions, revision checks, stable command IDs, explicit recovery and explicit import of the old queue. Restart and duplicate-command tests cannot silently resend an uncertain device effect or manufacture completion. |
| P1: Plan and integration | [PR #11](https://github.com/lbliii/tellyq/pull/11), `codex/m2-plan`; this plan and shared roadmap | Review independent and combined candidates, record precise checks and limits, and prepare the next acceptance session. |

L1 and C1 share small additions to Cast adapter and wire-contract files. Those
changes are coordinated by method/field ownership and tested together before PRs
are published. Neither branch imports code that exists only on the other branch.
S1 deliberately leaves the existing JSON CLI/session store in place. Switching
the application to SQLite belongs to runner integration, after review of this
foundation.

This batch does not enable automatic advancement. The current receiver leaves
ad state unknown, which blocks the strict playback/completion policy. Missing ad
metadata must not be relabeled as an inactive ad to make the queue move.

## M2a: establish what an ending actually looks like

After the first batch merges, run a supervised hardware session on the explicitly
selected receiver. Keep full records in ignored `runtime/`. Use at least two
known exact YouTube titles and record each launch, observation gap, operator
action and visible outcome. Short titles can reduce experiment time; their
published durations never establish completion.

| Task | Evidence required |
| --- | --- |
| L2: Three natural endings | Three observed endings across at least two real titles. Observe from launch through the terminal transition and any subsequent autoplay. Seeking near the end is diagnostic only and cannot count. |
| C2: Deliberate pause/resume | At least one counted run pauses and resumes through software; capture command receipts and subsequent observed states separately. A pause must not consume the item or start its successor. |
| L3: Interference ledger | Record ads, buffering, errors, manual stop, app exit and provider autoplay when observed. Mark scenarios not encountered; synthetic replay coverage does not become live evidence. |
| L4: Completion decision | Identify a correlated, fresh signal that distinguishes content completion from ads, cancellation, failure and takeover. Document its limitations and capability provenance, or retain an explicit blocker. |
| L5: Sanitized replay | Preserve the useful timing/order/identity relationships in reviewed fixtures, with origin and redaction documented. No local identifiers, addresses, credentials or raw runtime reports enter Git. |

A passive capture alone cannot prove that it observed the entire program or that
no other controller sought or stopped it. The acceptance record must include the
launch boundary and operator actions. Normalized records also cannot recover
fields already discarded by an adapter; investigate missing protocol signals at
that boundary if the first capture is inconclusive.

M2a passes only when the three runs and the completion decision are supported by
actual evidence. If completion remains unobservable, hold the queue and report
the specific limitation. Do not build a timer-based substitute.

## M2a repair batch after the first live checkpoint

The [checkpoint record](M2A-CHECKPOINT.md) preserves the three requested-title
runs, refused/rejected pause attempts, unknown ad state, missing terminal content
identity and subsequent content reusing a media-session ID. The control and
completion evidence streams below each branch directly from `7a08929`; each must
pass independently and target `main`, with a separate documentation PR.

| Stream | Branch / ownership | Deliverable |
| --- | --- | --- |
| C3: Control repair | [PR #15](https://github.com/lbliii/tellyq/pull/15), `codex/m2a-control-repair`; application/control paths, control contracts/reports, control tests and notes | Correct pre-boundary reset handling without weakening current-reset/ownership guards; expose safe typed rejection provenance; cover live-shaped failures offline. |
| L6: Completion evidence | [PR #16](https://github.com/lbliii/tellyq/pull/16), `codex/m2a-completion-evidence`; normalization diagnostics, lifecycle capture/replay, reviewed fixtures and notes | Preserve which relevant protocol fields were absent or malformed, cover same-media-ID content replacement, and document the remaining completion blocker or justified signal. |
| P2: Coordination | `codex/m2a-checkpoint-plan`; checkpoint ledger, roadmap, changelog and shared plan | Cross-review both code streams, test the exact combined tree and both merge orders, record final PR/CI evidence, and prepare the repeat live checkpoint. |

Shared model/adapter edits are coordinated by field/method ownership. No branch
depends on another repair branch. Normal checks remain offline. After merge,
run one short diagnostic with the new fields and control provenance before the
full live acceptance set. If the protocol still offers no supported completion
signal, record that limitation instead of repeating identical inconclusive runs.
No code-only result closes M2a or authorizes automatic advancement.

The final controls (`0f959b3`) and completion (`9071eb0`) heads passed independent
checks and macOS/Linux CI. Both merge orders produce the same tree. The exact
combined checkout passed `poe ci`: **531 tests**, **92.9% coverage**, lint/types,
source/wheel builds and isolated installation. The [checkpoint record](M2A-CHECKPOINT.md)
contains the review findings, CI links and remaining live gates.

## M2a metadata investigation after control acceptance

The repaired live run on `e4b2a91` passed pause/resume and cleanup. It received the
requested title's duration during playback, but FINISHED omitted media identity
and ad state remained unknown. Provider custom data was object-shaped; the shape
capture did not retain whether it was empty or what useful fields it contained.

| Stream | Ownership | Bounded deliverable |
| --- | --- | --- |
| Metadata capture | Pure provider diagnostic projection, opt-in passive collection, typed records and privacy/offline tests | Distinguish empty/nonempty containers and reviewed source paths without exporting arbitrary private payloads; leave playback policy unchanged. |
| Metadata research | Primary Cast/YouTube references, pinned library inspection, checkpoint/roadmap | Explain duration and incremental updates, separate standard semantics from YouTube observations, and record a concrete capability decision. |
| Coordination | Tool review, scoped schema run and focused numeric follow-up, exact combined validation | Start observer before each launch, preserve operator chronology, inspect terminal/subsequent state, and publish independent PRs against `main`. |

Each branch starts from `e4b2a91`. The coordinator alone owns live hardware commands.
The [research note](YOUTUBE-METADATA.md) distinguishes media-session identity alone
from ordered content history and explains why literal inactive-ad booleans are a
TellyQ policy choice rather than a universal protocol requirement. Any alternative
source-specific rule needs a justified contract and separate acceptance; this
batch does not relax the production gate.

Two scoped runs supplied a plausible path. The schema run identified numeric
`status.customData.playerState`; first-party YouTube source reads this field and
distinguishes ad-specific codes. A narrow numeric follow-up observed ordinary
startup/buffering/playing codes and zero on FINISHED, then code 5 with same-title
BUFFERING. It observed no ad-specific codes. Tooling at `3a60058`
([PR #18](https://github.com/lbliii/tellyq/pull/18)) passed 566 tests, 93.3% coverage,
lint/types/build/install and macOS/Linux CI. The live controller stayed on `e4b2a91`.

The next work identified by that investigation was a finite, source-qualified
YouTube state interpretation and ordered content correlation, with explicit
unknowns and offline regression cases before changing policy. Then run the full acceptance set. Another schema-only
capture or Lounge probe is not the immediate next step. The research note retains
Lounge as a bounded fallback if the candidate proves insufficient. No timer, UI,
custom receiver, MCP layer or new hardware is introduced to bypass the gate.

## M2a completion implementation

The [completion plan](M2A-COMPLETION-PLAN.md) starts from merged `fe73b6d` and keeps
two independently testable work streams: the coupled provider/domain completion
contract, and a checkpoint runner using existing application interfaces. The
coordinator owns source review, combined validation, live hardware and the ledger.

The adapter interprets only a finite set of source-qualified YouTube states. The
generic domain attributes qualified anonymous terminals through recent ordered
content history without filling missing identity into the original observation.
Ad context, unknown codes, contradictions, gaps, partial replay windows, changed
sources, replacement and stop intent cannot authorize a new completion.

A historical completion witness survives later observations independently of
current ownership. Routine observation windows now fit within the freshness
contract. The checkpoint runner records intent, receipts, all observations and
the first completion witness before guarded cleanup; it never advances a queue.

Exact candidate `2599345` passed 666 offline tests, packaging and installation,
then three natural endings across two titles with one verified pause/resume.
The user confirmed the pause and resume. A later ad-specific code and replacement
were captured; the original completion survived and cleanup refused replacement.
See the ledger for the nonzero cleanup outcome, visual evidence and untested cases.
PRs #20–22 merged at `6aec896`. The merged tree matches reviewed integration
`a82cb17` and passed 666 fresh offline tests. M2b is now underway under the
[independent work-stream plan](M2B-PLAN.md).

## M2b: one persistent playback owner

The storage foundation can proceed in parallel with M2a because it executes no
device effects. Automatic advancement starts only after the M2a evidence review.
The [M2b plan](M2B-PLAN.md) separates the first independent component PRs from
their subsequent CLI/IPC composition and hardware acceptance. The following work
is not functionality delivered by S1.

1. **R1 — Foreground runner and mailbox.** Add `tellyq serve` with one owner per
   receiver, a private local IPC endpoint and immutable status snapshots. A
   second owner fails clearly. Keep device effects on the owner thread; callback
   threads enqueue detached observations. Client exit does not end playback.
2. **R2 — Durable command execution.** Integrate the queue/journal and migrate
   legacy JSON explicitly. Commit intent before dispatch, record acknowledgement
   separately, and reconcile uncertain effects after restart before retrying.
   Never hold database locks while waiting on the receiver.
3. **Q1 — Evidence-driven queue policy.** Add a pure reducer using the accepted
   lifecycle signal. One terminal decision consumes one attempt once. Skip is
   distinct from finished. Provider autoplay and another controller taking over
   cause a hold/reconciliation rather than a competing launch.
4. **R3 — Cancellation and responsive reads.** Stop immediately cancels pending
   advancement; bounded in-flight I/O must not monopolize status or the local
   stop acknowledgement. Define clean shutdown and explicit timeouts before
   considering background startup.
5. **R4 — Crash and interruption replay.** Cover pre-dispatch,
   post-dispatch/pre-acknowledgement and post-completion restart points, duplicate
   terminal events, stale workers, long pauses, buffering, ads and disconnects.

Queue intent, observed playback and command execution remain different states.
SQLite transactions protect local decisions; they do not provide exactly-once
remote delivery to a Chromecast. Persisted monotonic observations never become
fresh evidence in a new process.

## First-batch validation

The reviewed code heads are controls `f4b7e59`, storage `3eda15b`, and lifecycle
`636542b`. Each branch is independent of the others. The two shared adapter/record
files merge cleanly in either controls/lifecycle order, producing the same tree.

The exact combined candidate at temporary integration commit `74b794d` passed
`uv run --locked poe ci`: **487 tests**, **92.8% branch-inclusive coverage**,
Ruff/format/ty, source/wheel builds and isolated installation/CLI smoke. A fresh
environment containing only the wheel also imported the new and existing core
modules with Cast, Zeroconf, Milo and Chirp absent, with socket operations blocked
and no runtime directory created. The runtime was standard CPython 3.14.0 with the
locked dependencies. Platform CI is attached to the PRs above.

Cross-review found and fixed three boundary issues: cancellation between items
and after settlement now prevents stale dispatch; unexpected SQLite schema
objects cannot alter queue mutations; interrupted transport callbacks survive as
explicitly partial diagnostic data and cannot create completion during replay.
Export also retains known launch-failure classifications while excluding arbitrary
diagnostics. All of these results are offline. No natural ending, live pause/resume,
handoff or stop-latency acceptance run occurred in this batch.

## Release gates and progress

| Gate | Required result | Current status |
| --- | --- | --- |
| Baseline | M1 merged and accepted | Passed: code `71a3c39`, docs merged at `8a480ea` |
| First batch | Independent PR checks plus exact combined lint/types/tests/build/install | PRs #11–14 merged; `7a08929` passed 487 tests, 92.8% coverage, lint/types/build/install |
| M2a | Three natural endings, two titles, one pause/resume, justified completion signal | Lifecycle gate passed on candidate `2599345`: three supported endings, two titles and verified pause/resume; ad/replacement behavior and remaining limits recorded; merged and checked at `6aec896` |
| Runner | One owner, responsive mailbox, durable intent, explicit recovery and cancellation | First component swarm underway; composition and hardware gate follow in [M2b](M2B-PLAN.md) |
| Queue acceptance | Three three-item runs, six correct handoffs, no early or duplicate advancement | Not run |
| Response bounds | Healthy-LAN stop accepted locally within one second; observed outcome within ten seconds or an explicit timeout | Not measured |
| Recovery | Restart scenarios reconcile uncertainty without a duplicate launch or false completion | Not run against the runner |

For each hardware run record actual handoff delays, observation gaps, versions,
failures and user-visible results. Keep unsuccessful runs in the ledger. Missing
completion evidence holds the queue and requests attention.

Use Python 3.14 with the locked dependencies and ordinary threads. Each code PR
runs `uv run --locked poe check`; package/dependency changes also run preflight.
The exact combined tree runs `poe ci`, including source/wheel installation, and
final PR heads need macOS/Linux CI. Tests block sockets and DNS. Free-threaded
compatibility, MCP, subscription services, recommendations and UI remain later
work; this milestone does not require a framework or new dependency.
