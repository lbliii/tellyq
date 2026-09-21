# M2 closeout, service acceptance and recovery

**M2 is closed with a known reliability limitation, by user approval on
2026-09-21.** The implemented YouTube queue, controls, persistence and recovery
are delivered. The intermittent acknowledged enqueue stall remains open as
[issue #42](https://github.com/lbliii/tellyq/issues/42). This is a qualified
milestone closeout, not a claim that the defect is fixed or that dependable
unattended use has been established. M3's Milo/MCP work may proceed.

## Aggregate evidence and scope decision

The [native-runner checkpoint](NATIVE-RUNNER-CHECKPOINT.md) at `28d043d` contains
one completed three-item session and one stalled session. The
[enqueue investigation](ENQUEUE-INVESTIGATION.md) at `b0e32b1` adds two completed
three-item sessions. Production playback code is identical between those
revisions; changes were documentation, checkpoint-driver exit reporting and its
tests. The later runs add response-only logging, with no extra provider requests
or playback commands. Logging can affect timing, which remains a stated limit.

The combined record is **three successful three-item sessions out of four
attempts: seven verified handoffs out of eight planned**. Six of those handoffs
belong to the three completed sessions; the stalled session contributes one
successful handoff and one failure. No failures are removed from the record.

The original milestone required three completed sessions, but did not require
them in one new batch or forbid diagnostic logging. The earlier statement that
the two later comparisons could not count was too restrictive and is superseded
by this aggregate assessment. It does not dismiss the intermittent reliability
defect. New event readers, authoritative playlist readback and exhaustive provider
investigation are not additional M2 exit requirements.

| Original area | Delivered evidence | Remaining limit |
| --- | --- | --- |
| Natural completion and advancement | M2a observed three endings across two titles; the composed native runner completed three three-item sessions across the four-attempt record | One acknowledged successor did not play; issue #42 remains open |
| Controls | Pause/resume verified in a completed native session; active cancellation with a successor staged prevented its playback during a 110.304-second watch; observed active stop took 4.261 seconds | A bounded observation window does not prove an empty remote playlist or disabled autoplay |
| Recovery and no duplicate dispatch | Offline crash-boundary coverage; live owner loss followed by fresh approved-successor reconciliation, with unchanged command/operation history; one initial START per native trial | A missed predecessor ending stays unknown and further staging holds; no remote exactly-once guarantee |
| Human confirmation | Earlier visible pause/resume and cleanup; latest distinct-video run had user-confirmed advancement and audible nature sound | Separate visual confirmation was not recorded for every transition in every run |
| Engineering checks | Tested `b0e32b1` passed 1,355 offline tests, Ruff, formatting and ty; investigation PR #41 passed macOS/Linux CI including build/install checks | Automated tests do not substitute for live provider evidence |

The earlier service checkpoint and its 10.43117-second client-visible stop miss
remain in [M2B-CHECKPOINT.md](M2B-CHECKPOINT.md). Later stops do not erase that
measurement. Known ad, takeover, readback and receiver-autoplay limits remain in
the linked records. Acknowledgement, local ticket completion, receiver observation
and human observation continue to be distinct facts.

## Delivered behavior and follow-up boundary

The opt-in native runner accepts an explicit queue of YouTube IDs, starts the
first item, stages at most one approved successor and verifies observed playback
before adopting it. A persistent Python 3.14 owner serves local start/status/pause/
resume/stop commands and persists queue and effect history. Status and recovery
do not blindly replay effects. One previously approved successor may continue
after disconnection; reopening reconciles it while holding further staging.

Native mode remains explicit; legacy manifests retain their existing behavior.
Adjacent identical IDs are declined. The controller does not infer completion
from duration or treat a successful enqueue as playback. Unexpected content or
lost ownership prevents adoption and unsafe control.

Further investigation of issue #42 is bounded bug work, not a new milestone
feature or a prerequisite to building M3. M3 should expose the existing outcomes,
holds and uncertainties through Milo/MCP without changing playback policy.
Broader reliability validation remains in M8; M2 closure does not satisfy that
personal-release gate. No additional live test was run for this documentation
closeout, and the detailed historical records below remain intact.

## Offline process-loss coverage

The recovery production head `293e9fe` passed locked Python 3.14 preflight with
994 tests, Ruff, formatting, ty, source/wheel builds and isolated installation.
CI then exposed a scheduling race in the synthetic checkpoint fixture. Test-only
fix `4e48a9b` synchronizes each ending with client-observed verified playback and
adds slower polling coverage: 20 repeated covered runs and all 995 independent
tests pass, along with macOS/Linux CI. The final local combined candidate passes
1,090 tests and packaging checks. The full [validation history](M2B-PLAN.md#ci-scheduling-regression-and-final-validation)
retains the earlier CI failures. The three-item checkpoint remains synthetic evidence.

The ordinary test suite launches isolated synthetic service processes. Each child
allows only AF_UNIX sockets and denies DNS; pytest's global socket/DNS prohibition
is unchanged. The composed paths use the actual CLI, private IPC, owner, playback
task, SQLite store and queue executor. The backend records synthetic effects in an
fsynced ledger before returning a receipt.

| Crash boundary | Durable evidence before exit | Required restart behavior |
| --- | --- | --- |
| Before dispatch | A prepared PENDING start intent; no effect | Keep the intent fenced; no start or next item |
| After effect, before acknowledgement | DISPATCHED start and one recorded synthetic effect | Convert uncertain delivery to UNCERTAIN; never resend |
| After durable completion | First item FINISHED, before release or successor dispatch | Preserve FINISHED; cancel automatic continuation |
| Cancellation | Explicit IPC stop committed queue cancellation, before remote stop | Keep cancellation; never replay start or launch a successor |
| Takeover | IPC reports lost ownership after a foreign receiver session appears | Hold without release; recovery marks unresolved work for attention |

`os._exit` terminates the process at these boundaries, bypassing service cleanup.
The next service recovers the same SQLite state and stale endpoint. Merely opening
it produces no synthetic effect. Retrying the original stable CLI start and sending
a different start ID both leave the effect ledger unchanged and the second item
pending. The preexisting three-item scenario also verifies normal synthetic starts,
releases, settlement and a clean restart. These are bounded fault models, not a
remote exactly-once delivery guarantee.

## Explicit service checkpoint

`scripts/checkpoint_service.py` attaches to an existing foreground owner. It never
discovers receivers, opens a Cast backend, starts a service, changes a manifest,
restarts a process, or falls back to legacy direct commands. Select the service
runtime and its exact manifest explicitly. The driver compares queue ID, stable
target ID, route, item identities, content and order against live IPC status before
submitting each command. Start requires a fresh pending queue.

Run from the repository root. Keep the manifest, owner runtime and checkpoint
output under ignored `runtime/`. Use the same owner runtime for the receiver and
stop the previous owner before opening another; separate runtimes do not coordinate
ownership of one TV. The output directory must be new and is never overwritten.
`--code-revision` is the operator's recorded tested revision, not an independently
verified source-tree claim.

Read-only diagnostics, including an existing held queue:

```sh
uv run --locked python scripts/checkpoint_service.py trace \
  --runtime runtime/service-owner \
  --manifest runtime/session.json \
  --output runtime/checkpoints/service-trace-01 \
  --code-revision TESTED_HEX_COMMIT --seconds 30
```

An explicitly authorized three-item run, with guarded cleanup stop if it exits
before verified final release:

```sh
uv run --locked python scripts/checkpoint_service.py start \
  --runtime runtime/service-owner \
  --manifest runtime/session.json \
  --output runtime/checkpoints/service-session-01 \
  --code-revision TESTED_HEX_COMMIT --seconds 900 --cleanup-stop
```

`start` is the explicit playback action. `--pause-hold 5` requests one pause after
verified initial playback and resumes five seconds after the client first sees a
verified pause. `--cancel-after 20` submits stop at or after twenty client elapsed
seconds; it does not infer content completion. Use the latter for a separate
interruption session so the three complete sessions remain measurable. Do not use
nominal duration as a completion signal. An uncertain command submission is never
retried automatically, including a stop whose acknowledgement was lost.

Trace mode accepts none of these effect options and sends status requests only.
The private journal preserves complete IPC status, including additive handoff
diagnostics when the owner supports them. The sanitized summary reports whether
those diagnostics were available. Read the private trace for the exact hold reason;
the summary deliberately does not copy arbitrary diagnostic strings or identifiers.

## Timing and interpretation

All timings use one client monotonic clock; the summary records its resolution and
the requested poll interval (default 100 ms). No cold CLI subprocess is started per
poll. Each IPC call has a one-second timeout. Observation and cleanup windows are
bounded, although an in-flight polling batch can finish after its deadline. The
reported elapsed time is the actual measured interval, including journal writes,
control submission and cleanup. The journal is private, durable evidence; its I/O
can delay the next poll.

For every command the summary distinguishes:

- `acknowledgement_ms`: measured from immediately before submit until its response.
  An accepted response means local mailbox acceptance only.
- `ticket_completed_ms`: first observed terminal ticket state, measured from the
  same submission origin. `handled` alone proves no receiver effect.
- `first_verified_state_ms`: first matching fresh verified status received by this
  client, measured from that same origin. Receipt request ID must match the
  canonical ticket ID and intended attempt; failed/canceled tickets cannot supply
  verification. Unknown remains null. This includes IPC
  and polling delay; it is not reconstructed physical receiver latency. Late
  observations are never backdated to meet a threshold.

The local stop acknowledgement target is one second; the observed stop target is
ten seconds. Compare the recorded values directly to those thresholds and retain
misses. The earlier 10.43117-second sample therefore remains a miss; changing the
driver does not alter that measurement.

Item evidence uses manifest positions, including repeated titles. The summary
counts sampled verified transitions only when the previous item has a durable
FINISHED projection and the next item has verified playback. A missed short-lived
state remains unknown. `distinct_attempts_observed` counts sampled identities;
**neither it nor the handoff count proves absence of unobserved duplicate effects.**
Audit the private durable command journal and receiver chronology separately for
the no-duplicate-start requirement.

The script writes a private `journal.jsonl` plus an allowlisted `summary.json` with
no target, address, queue/item/attempt IDs, private paths or raw diagnostic text.
`visual_confirmation` remains null and `live_acceptance` remains false: one tool run
cannot close the aggregate milestone. Record human evidence and reviewed session
results in the acceptance record separately. A zero exit code reports the selected
checkpoint operation's observed outcome, not milestone acceptance.

Cleanup is opt-in through `--cleanup-stop`. It uses the service's existing ownership
and cancellation guards and waits up to `--cleanup-seconds` (default 15, maximum
30) plus bounded in-flight calls. An already finished queue with verified release
needs no extra stop. Failed or unconfirmed cleanup stays explicit. The driver never
kills the owner, removes its locks, deletes evidence, or sends a replacement start.
Shutdown remains a separate explicit operation and does not itself stop playback.
