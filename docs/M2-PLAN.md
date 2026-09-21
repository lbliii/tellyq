# M2 execution plan: observe endings, then run a queue

Started 2026-09-21 from `main` at `8a480ea`, after the M1 acceptance record merged.
M1 passed on code commit `71a3c39`; its verified start/status/stop behavior is the
regression baseline. M2 adds a reliable unattended YouTube queue, in two stages.

## First batch: prepare lifecycle experiments and durable state

Three agents work in separate worktrees, each branched from `8a480ea`. Each PR
targets `main` directly and must pass without another agent's branch. The
coordinator reviews shared contracts, combines the exact commits in a temporary
checkout, runs the full checks, and publishes a separate planning PR. Agents do
not control hardware or merge their PRs.

| Stream | Branch and ownership | Measurable deliverable |
| --- | --- | --- |
| L1: Continuous lifecycle capture | `codex/m2-lifecycle`; new lifecycle module, capture script, focused tests and lifecycle notes; narrow additive adapter changes | A bounded read-only capture preserves normalized observations across a whole program, writes incremental private records, survives interruption, and exports explicitly labeled sanitized fixtures. Replay tests distinguish unknown, ads, buffering, pause, app exit, takeover and terminal evidence. |
| C1: Pause and resume | `codex/m2-controls`; playback contracts, Cast normalization/controls, application/CLI and focused tests | Explicit commands require current ownership and advertised or verified support. Command acceptance and observed effect remain separate. Unknown support, stale identity and takeover refuse the effect. Existing start/status/stop behavior remains covered. |
| S1: Durable queue and command journal | `codex/m2-storage`; new queue values/port, SQLite adapter, tests and storage notes | Atomic queue/attempt/intent transactions, revision checks, stable command IDs, explicit recovery and explicit import of the old queue. Restart and duplicate-command tests cannot silently resend an uncertain device effect or manufacture completion. |
| P1: Plan and integration | `codex/m2-plan`; this plan and shared roadmap | Review independent and combined candidates, record precise checks and limits, and prepare the next acceptance session. |

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

## M2b: one persistent playback owner

The storage foundation can proceed in parallel with M2a because it executes no
device effects. Automatic advancement starts only after the M2a evidence review.
The following work is a subsequent batch, not functionality delivered by S1.

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

## Release gates and progress

| Gate | Required result | Starting status |
| --- | --- | --- |
| Baseline | M1 merged and accepted | Passed: code `71a3c39`, docs merged at `8a480ea` |
| First batch | Independent PR checks plus exact combined lint/types/tests/build/install | Agents dispatched; validation pending |
| M2a | Three natural endings, two titles, one pause/resume, justified completion signal | Not run |
| Runner | One owner, responsive mailbox, durable intent, explicit recovery and cancellation | Subsequent batch |
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
