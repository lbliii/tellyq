# M2b: persistent ownership and durable queue execution

Started 2026-09-21 from merged `main` at `6aec896`. PRs #20–22 are merged.
This tree is identical to the final reviewed M2a integration tree `a82cb17`, and
its fresh Python 3.14 `poe check` passes 666 tests, Ruff, formatting and ty.
[Merged-main macOS/Linux CI](https://github.com/lbliii/tellyq/actions/runs/35638183132)
also passed.
M2a's three natural endings across two titles and verified pause/resume remain
the live evidence baseline. Pulling the merged code did not trigger another
hardware test.

## Outcome and sequencing

M2b adds one persistent playback owner, durable queue execution and recovery,
then proves automatic handoffs. The first wave below implements independently
testable components against the same `main` commit. None imports code from another
unmerged branch. A later composition wave connects them to the foreground CLI and
private local IPC; the components alone do not enable automatic playback.

This split keeps the user's requested independent PRs while giving each stream
clear ownership. Shared contract changes require coordination. Generic policy,
local ownership and durable execution stay separate from YouTube interpretation.

## First-wave work streams

| Stream | Branch and file ownership | Measurable deliverable |
| --- | --- | --- |
| Q2: Pure queue decisions | [PR #25](https://github.com/lbliii/tellyq/pull/25), `codex/m2b-queue-policy`; new `tellyq/domain/queue_policy.py`, focused domain tests and `docs/QUEUE-POLICY.md` | Decide settlement, hold, next eligible item or completion from existing immutable queue/playback evidence. One ending yields one decision; cancellation, uncertainty and takeover cannot launch a next item. |
| R2: Durable dispatch and recovery | [PR #24](https://github.com/lbliii/tellyq/pull/24), `codex/m2b-durable-execution`; new `tellyq/queue_execution.py`, focused tests and `docs/QUEUE-EXECUTION.md`; documentation-only clarification of `QueueStore.mark_dispatched` | Commit intent and win one dispatch claim before a bounded supplied device operation; record receipt separately. Repeated IDs, failed persistence and reopened databases never silently repeat an uncertain effect. |
| R1/R3: Owner and mailbox | [PR #26](https://github.com/lbliii/tellyq/pull/26), `codex/m2b-owner-mailbox`; new `tellyq/runner.py`, focused runner tests and `docs/RUNNER.md` | One non-daemon worker owns task creation, effects and close. Bounded mailbox, cached immutable status, priority stop/cancellation and explicit shutdown timeout; competing owners fail. |
| Coordination | [PR #23](https://github.com/lbliii/tellyq/pull/23), `codex/m2b-plan`; this plan, roadmap, architecture, acceptance notes and changelog | Review interfaces and failure boundaries, test independent and exact combined heads, and publish separate PRs targeting `main`. |

All agent code runs offline. The coordinator owns integration and publication;
no agent discovers devices, plays media or changes private legacy queue/session
state. Agents use separate worktrees and editable environments.

## Contracts to preserve

### Decisions do not execute effects

The pure queue policy consumes existing `QueueSnapshot`, `SessionSnapshot` and
qualified `CompletionEvidence`. It receives current time and process-local owner
authority as inputs, never reading a clock or creating IDs itself. Receiver time,
duration, PAUSED, ads and an accepted command are not program completion.

Settling an observed ending and permission to launch the next item are distinct.
The policy returns a settlement first; the caller persists it, reloads the queue
and evaluates again. A stop arriving between those steps cancels advancement.
Historical completion followed by replacement may settle the old item, but the
replacement must hold the queue. Fresh current evidence is required before a
new launch. Explicit skip is separate from finished and never implies a remote
stop occurred.

Cross-component review identified a handoff boundary for the composition wave:
`prepare_stop` cancels the queue, accepts only an unresolved attempt, and prevents
that attempt from being settled as FINISHED. Operator stop therefore cannot double
as between-item receiver cleanup. When an ending is followed by unqualified
buffering, the reducer must hold. The composed implementation needs a separately
reviewed owned-session handoff/reconciliation contract if fresh terminal or idle
evidence is unavailable; it must not weaken operator cancellation or unknown-ad
rules to force the next launch.

### Durable local claims do not promise remote exactly-once delivery

The executor reuses the existing SQLite store, including its strict atomic
PENDING-to-DISPATCHED claim. Only the winner can invoke a supplied operation.
Preparing the same command twice is idempotent local history; it is not permission
to send the command twice. Commit acknowledgement separately from observed effect
and queue settlement. No database transaction spans device I/O.

Validate command/receipt identity, generation, queue revision and cancellation at
the relevant boundaries. Failure after dispatch may mean the TV acted. Preserve
that uncertainty and require reconciliation; never automatically resend it after
restart. Exclusive runner ownership remains necessary around the unavoidable gap
between a local commit and a remote effect. Persisted monotonic evidence cannot
become fresh process-local authority after restart.

### A responsive mailbox has one device owner

The owner task is constructed, used and closed on one managed worker thread.
Clients submit immutable commands and read cached immutable snapshots. Status
must not wait for a device call. Stop sets a cancellation latch immediately and
has priority over queued launches; the operation executing on the owner thread
must check that latch at its effect boundary. No client thread controls the
receiver directly.

The initial owner retains both a private receiver lock and the legacy runtime's
`command.lock`, preventing old direct commands from competing with it. This is a
conservative single-runtime restriction; private IPC in the next wave provides
status/stop access while that lock is held. The API is not yet `tellyq serve`.

Shutdown requests cancellation and waits for a bounded interval. A timeout must
report a still-running owner and retain its locks until the worker actually exits.
The caller must not close the backend from another thread or pretend a blocked
worker has stopped. Permanent cancellation ends that runner's session; any new
session requires an explicit new owner.

## Independent and combined validation

Each code branch runs locked Python 3.14 `poe check`; packaging checks run for new
modules. Ordinary tests keep socket and DNS access blocked. No new framework or
dependency is required, and standard GIL-enabled Python remains the supported lane.

The meaningful failure cases include:

- Duplicate endings, completion plus replacement in one batch, stale receiver
  evidence, ads, pauses and buffering; none can produce an early or repeated launch.
- Cancellation between settlement and next selection, while queued, and after
  dispatch preparation; status remains readable while the owner is busy.
- Failures before dispatch, after dispatch but before acknowledgement, and during
  outcome persistence; reopened SQLite state retains uncertainty without retry.
- Competing owner processes, bounded mailbox overflow, task errors and shutdown
  timeout; no premature unlock or second effect owner.

The coordinator reviews the actual final APIs together, then tests all exact heads
in an isolated integration checkout with `poe ci`, including build and isolated
installation. Both independent and combined results are recorded before acceptance.
Passing component tests is not a claim of measured live stop latency or handoffs.

## Composition wave and hardware gate

After the first-wave PRs merge:

1. Compose a bounded playback task from `PlaybackApplication`, the queue reducer
   and durable executor. Establish ownership and reconcile on startup. A deliberate
   foreground command starts the owner; importing or viewing state does not.
2. Add `tellyq serve` and a private local IPC client using shared typed command and
   response contracts. Legacy JSON import stays explicit. CLI clients may exit
   while the owner continues observing playback.
3. Connect one verified completion to one durable settlement and eligible next
   item. Recheck cancellation/current ownership before dispatch. Provider autoplay
   or another controller taking over holds the queue for attention.
4. Run end-to-end offline crash, mailbox and fake-backend scenarios, including
   healthy responsiveness and delayed/failing operations.
5. Request the planned live checkpoint only after the combined implementation is
   ready: three three-item sessions and six correct handoffs, no duplicate starts,
   plus interruption and recovery checks. Record actual local stop acknowledgement
   and observed stop latency separately from their one-second/ten-second targets.

M2b closes only after that composed behavior passes. MCP, subscription services,
Cueby's recommendations and the optional Chirp interface remain later milestones.

## First-wave review and validation

All three implementation PRs start directly from `6aec896`, target `main`, and
pass independently. They add no dependency and do not alter the CLI or live Cast
adapter. The only existing-code-file change outside the new components is a
clarifying docstring for the already-strict `QueueStore.mark_dispatched` claim.

| Component | Reviewed commit | Independent validation |
| --- | --- | --- |
| Queue policy | `24faf2e` | 755 tests, including 89 new cases; Ruff/format/ty, source/wheel builds and isolated installation |
| Durable execution | `d4522ba` | 717 tests, including 51 new real-SQLite boundary cases; lint/types/build/install |
| Owner and mailbox | `578cb91` | 692 tests, including 26 event/barrier/subprocess cases; lint/types/build/install |
| Exact combined candidate | `8c35286f5e85edabf56d3eb178be6b68cea50946` | `poe ci`: 832 tests, 93.9% branch-inclusive coverage, Ruff/format/ty, source/wheel builds and isolated installation |

The combined candidate's tree is `6ffc526d5bf93159f95de0b4a64a46c801b86181`.
Later plan-only edits record these results; the tested component commits above
remain unchanged. No hardware operations occurred and existing private runtime
queue/session files were not migrated or rewritten.

Cross-review fixed mailbox command-ID collisions across regular, active and
reserved stop slots. Tests now reject the same known ID describing a different
action before cancellation or queuing. Independent owner review found no further
actionable issue and reran all 26 focused tests. Policy review tightened idle
ordering and ties a persisted FINISHED decision back to its original live witness.
A real SQLite bridge verifies settlement idempotence and cancellation after
settlement but before next selection. Executor review verifies receipt identity,
a START-only cancellation guard and explicit uncertainty after persistence failures.

These are component acceptance results. They do not establish a running IPC
service, composed queue recovery, measured live stop latency or automatic handoffs.
The composition wave above is still required before M2b can close.

## Composition implementation and offline checkpoint

The first wave merged at `9715b58`; a fresh baseline check passed 832 tests.
The continuation uses isolated branches from that same main, with two component
PRs and a service integration PR all targeting `main`:

| Stream | Reviewed component | Independent validation |
| --- | --- | --- |
| Concrete playback task and handoff | [PR #28](https://github.com/lbliii/tellyq/pull/28), `44f92f08b6d096001a380be7c7866742370e351a` | 875 tests, Ruff/format/ty, wheel/sdist and isolated installation |
| Private local IPC | [PR #27](https://github.com/lbliii/tellyq/pull/27), `4de6e7647e6ce976217256cb7e718c17b0658c46` | 887 tests, Ruff/format/ty, wheel/sdist and isolated installation |
| Foreground composition | [PR #29](https://github.com/lbliii/tellyq/pull/29), includes both exact component commits | 965 tests, 90.1% branch-inclusive coverage, lint/types, wheel/sdist and isolated installation |

The service integration includes both component histories so its head is usable
and testable on its own. Merge the two component PRs first for smaller reviews;
the integration also targets main. No public PR is merged by the agents.

`tellyq serve` validates an explicit queue manifest, acquires owner locks, opens
the backend on the owner thread and publishes private IPC after a queue view is
available. It waits for explicit start. Existing control names route to the owner
when its endpoint exists; connection failure never opens another backend. Ticket
acceptance/handling, command receipts, observed state and historical completion
remain separate. Legacy JSON import is explicit and leaves source files intact.
See [FOREGROUND-SERVICE.md](FOREGROUND-SERVICE.md) for commands and limits.

The task now journals PAUSE/RESUME and a distinct internal RELEASE for finished
attempts. Release does not cancel the queue or rewrite FINISHED. Next-item start
requires observed idle after release, durable acknowledgement, retained live
authority and another check immediately before the effect. Receiver replacement,
positive ads, replay/reset, cancellation, stale evidence or persistence failure
hold the queue. Reopening durable history never restores owner authority.

Review fixed a STOP transaction boundary, lost replacement/ad events before a
successor start, dropped baselines on refused controls, changed-intent command
retries, and cleanup that could truncate the final IPC shutdown response. Service
cleanup also joins its threads if stdout fails during a shutdown timeout. Default
Cast integration rejects non-video content kinds before connecting because the
adapter normalizes YouTube observations as video identities.
Linux CI exposed a startup-failure readiness race while the worker was still
releasing resources; a deterministic regression now requires joining that worker
before reporting its final failure state.
Persistent Cast observation now retains only the latest raw window and at most
six private identity witnesses. Equivalence tests compare takeover decisions
against full history over arbitrary scope boundaries; returning to an old identity
cannot erase a takeover, and a new connection still invalidates old scopes.

The combined scenario runs actual CLI/AF_UNIX IPC, owner, task and SQLite with a
synthetic receiver: three items finish, three starts and three releases occur,
retrying start adds no effect, shutdown closes resources, and reopening the queue
holds without replay. Internet sockets/DNS remain forbidden. Both component PRs
also pass their GitHub Python 3.14 macOS/Linux workflows.

**M2b live acceptance remains pending.** No hardware ran during this wave. The
actual M2a FINISHED → same-title code-5 BUFFERING/position-zero sequence is a
regression that must HOLD, not a supported handoff. A bounded reinspection of the
pinned first-party source found no field-specific meaning for this sequence;
IFrame API numeric values were not substituted. The next supervised checkpoint
tests foreground ownership/status/stop/shutdown first, then attempts supported
handoffs. Three three-item live sessions and six handoffs are still required to
close the milestone.

## Closing workstreams

PRs #27–29 are merged at `b4ff7d3`. The
[foreground-service checkpoint](M2B-CHECKPOINT.md) passed ownership, playback,
durable completion, cancellation and status-only reopen checks. Automatic release
held, so no successor started. Active-stop acknowledgement was below one second;
the measured client-visible outcome was 10,431.17 ms, above the ten-second target.

The closing swarm starts three independent branches from that exact `main`.
Every PR targets `main`; no branch requires an unmerged sibling to pass its tests.

| Workstream | Branch | Deliverable and review gate |
| --- | --- | --- |
| Handoff diagnosis | [PR #31](https://github.com/lbliii/tellyq/pull/31), `codex/m2-handoff-diagnostics` | Typed, sanitized hold reasons and evidence through cached status; replay actual ending/reset shapes and retain takeover/ad vetoes. Repair only behavior supported by evidence. Unknown code 5 is not reinterpreted by numeric coincidence. |
| Stop responsiveness | [PR #30](https://github.com/lbliii/tellyq/pull/30), `codex/m2-stop-latency` | Return once sufficient correlated stop evidence is available, preserving the observation deadline, ownership checks and queued contradictions. Keep command acknowledgement separate from verified outcome. |
| Recovery and acceptance | [PR #32](https://github.com/lbliii/tellyq/pull/32), `codex/m2-recovery-acceptance` | Composed crash-boundary tests and an explicit, bounded service checkpoint tool with reproducible timing and a sanitized record. No hardware effects in normal tests. |

Shared protocol/JSON changes are coordinated, and the exact combined candidate
must pass lint, formatting, types, offline tests and packaging/install checks.
Agents do not discover or control devices. The next hardware checkpoint is a
separately requested, supervised run after review; publishing these PRs alone does
not accept M2.

Acceptance proceeds in this order:

1. Inspect one fresh natural ending's exact hold/eligibility evidence. If it holds,
   keep the queue safe and fix the supported cause; do not retry indefinitely or
   bypass an unknown provider state to force a handoff.
2. Demonstrate one supported automatic A → B handoff with no duplicate dispatch.
3. Measure foreground active stop: local acknowledgement within one second and
   the first client-visible verified outcome within ten seconds. Record actual
   polling resolution and distinguish receiver timestamps from client timings.
4. Complete three three-item sessions with six correct automatic handoffs,
   foreground pause/resume and cancellation that prevents further advancement.
5. Exercise pre-dispatch, post-dispatch/pre-acknowledgement and post-completion
   interruption/reopen boundaries. Retain uncertainty and never replay device
   effects silently. Record which cases use a synthetic backend and which are live.

The final commit-specific acceptance record must identify the tested revision,
observed transitions, delays, recovery outcomes, visual confirmations and remaining
limits. M3 CLI/MCP work begins after this gate, not as a substitute for it.


## Closing-stream validation

The independently tested heads were combined without conflicts in an isolated
checkout. Exact code candidate `98ec6702daf48f3d6db69c8a0ee45436ccc8df66`, tree
`e9d120b979207f717dbcb9f6dd58a132f4bafd9e`, passes locked Python 3.14 `poe ci`:
1,089 tests, 90.7% branch-inclusive coverage, Ruff, formatting, ty, source/wheel
builds and isolated installation.

| Independent PR | Tested code head |
| --- | --- |
| [Stop responsiveness #30](https://github.com/lbliii/tellyq/pull/30) | `7e8d8014bdad95feaebf41098204b232c8d34e10` |
| [Handoff diagnostics #31](https://github.com/lbliii/tellyq/pull/31) | `db3322484cbce791e666b86c2d41dc35feacc30b` |
| [Recovery and acceptance #32](https://github.com/lbliii/tellyq/pull/32) | `293e9fe0fe0f20de14f1988c578de2df22c72544` |

The recovery branch independently passes 994 tests and preflight. Its private
service checkpoint correlates command timing to the canonical ticket and attempt;
failed/canceled tickets and another client's same-action receipt cannot supply
verification. Five actual process-loss scenarios preserve uncertain work without
replay, and a synthetic three-item service run observes two handoffs. See the
[acceptance procedure and evidence limits](M2-ACCEPTANCE.md).

PRs #30 and #31 also passed their Python 3.14 macOS/Linux GitHub workflows.
This record is a documentation-only follow-up to the exact tested code heads.
No hardware ran for these closing streams. The live three-session gate above and
the previous measured stop-target miss remain open; test success does not accept M2.
