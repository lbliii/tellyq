# Native foreground runner checkpoint

The merged native runner at `28d043d` passed one complete A → B → A session,
active cancellation with a successor staged, and process-loss recovery into the
already approved B. A second full-session attempt stalled after B despite an
acknowledged enqueue for the final A. **M2 remained open at this checkpoint.** Three verified handoffs
were observed across the two attempts; the required three successful three-item
sessions and six handoffs were not achieved.

The subsequent [enqueue investigation](ENQUEUE-INVESTIGATION.md) at `b0e32b1`
passed a repeated and a distinct three-item comparison with response-only
instrumentation. Those four additional handoffs did not reproduce or explain the
failure below. The later [M2 closeout](M2-ACCEPTANCE.md) counts three completed
sessions across the combined four-attempt record and closes M2 with a known
reliability limitation. This failed attempt remains tracked in
[issue #42](https://github.com/lbliii/tellyq/issues/42).

These supervised checks ran on 2026-09-21 with Python 3.14.0 and the locked
PyChromecast 14.0.10/casttube 0.2.1 dependencies. Actual merged `main` passed all
1,352 offline tests, lint, formatting, ty, source/wheel builds and isolated install
before the TV runs. Hardware runs used the unchanged merged playback code through
the foreground owner, private IPC, SQLite journal and service checkpoint client.
Private manifests, device identity and complete journals remain under `runtime/`.

## Selection and results

The [quiet nature selection](LIVE-TEST-MEDIA.md) was A, RSPB's Abernethy Forest,
and B, RSPB's Insh Marshes. The three-item manifests used A → B → A with distinct
item IDs. Completion came from receiver evidence, never from their approximately
95-second metadata duration.

| Attempt | Receiver and durable evidence | Outcome |
| --- | --- | --- |
| Session 01 | Three qualified natural endings; exact advancing non-ad playback for all three items; two native handoffs. One START, two PLAY_NEXT operations, one pause, one resume and a separate final STOP. | Passed this individual sequence. Final stop observed in 2.355 seconds. |
| Session 02 | A finished and B was verified. B finished; the final A remained pending despite the second PLAY_NEXT being acknowledged. No final-A attempt or playback was fabricated. | Held. After 141.248 seconds from B's sampled finish to stop submission, the operator interrupted collection and guarded cleanup succeeded in 4.563 seconds. |
| Active stop | A was verified and B was durably staged. Cancellation preceded CLEAR and the separate STOP. A became stopped; B and the third item remained pending. | Passed. Local stop acknowledgement: 0.690 ms; verified stop: 4.261 seconds. |
| Reconnect | After verified A and an acknowledged reservation for B, only the test controller process was killed. After a 55-second deliberate gap and owner startup, fresh B playback was verified with unchanged command/operation history. | Passed bounded recovery. A remained needs-attention because its ending was missed, B became current, and the third item held. Final stop observed in 2.663 seconds. |

The third planned full-sequence trial was not run after the repeatability failure.
The independent cancellation and reconnect checks continued. No extra START or
enqueue retry was used to rescue the held second session. Audited durable history
contains exactly one initial START per trial; native successors have native
provenance rather than manufactured START receipts.

Pause and resume in session 01 were independently verified. Client-observed
latencies were 10.535 and 10.076 seconds respectively; these were not instantaneous
controls. Initial playback verification took about 38–40 seconds in the first
three trials. All measured stops were below the ten-second target and local stop
acknowledgements below one second; retain the distinct timing categories rather
than treating a fast acknowledgement as physical playback control.

## Cancellation and recovery limits

Following active stop, 110.304 seconds of passive IPC observation yielded 218
samples and 49 distinct receiver observation timestamps. No successor playback,
ownership loss or receiver reactivation was observed. CLEAR acknowledgement plus
verified receiver-app exit establishes that combined outcome over this window;
it does not independently prove an empty remote playlist or disabled autoplay.

Reopening the interrupted owner sent no START, PLAY_NEXT or CLEAR. The approved B
was reconciled from fresh exact identity and non-ad progress. The durable hold
remained set and cancellation remained false until the explicit cleanup STOP.
The observed B does not prove that A ended normally during the gap. Final cleanup
was verified, all checkpoint owners exited, and the user explicitly confirmed the
Chromecast home screen after that final cleanup.

The user also reported extremely quiet clips and could not determine whether
audio was playing. TellyQ issued no mute or volume commands. Audible output remains
unconfirmed; these clips were selected for nature footage without narration.
No explicit visual confirmation of every handoff or pause/resume was recorded.
Receiver playback, visible TV behavior and audible sound remain separate evidence.

## Findings and follow-up

The second session demonstrates the unresolved delivery boundary: a guarded
library call returned without a scope-change veto, yet the requested successor
was not subsequently verified. The receiver continued reporting B's ended state.
There is no authoritative remote queue-membership record for this attempt, so the
evidence does not distinguish a mutation that failed to take effect from a queued
item that YouTube did not advance to. Repeat-ID handling, remote queue state and
timing are hypotheses, not established causes. There is no basis to label this an
ad failure from the sampled service evidence.

The next focused diagnostic should capture sanitized mutation-result and ordered
receiver evidence, and compare a distinct third video with the repeated-A case.
Keep any queue readback experimental: earlier readback failed and library binding
can have side effects. Preserve the existing one-successor authorization, no-retry
rule and exact playback checks. Do not bypass the hold with a blind second START.
Repeat the full three-session gate after the handoff failure is explained or fixed.

This checkpoint also exposed a client reporting omission: `queue_finished` is the
new native completion result, but the script accepted only legacy completion and
explicit-stop results as successful exit codes. Session 01 therefore exited 1
despite correctly recorded completion and cleanup. The follow-up fix recognizes
native completion while retaining the verified-start and requested-cleanup checks;
its offline regression cases keep unverified start or cleanup nonzero. Historical
live exit codes and failed attempts are retained unchanged. This reporting fix
does not address the second session's missing successor.

The follow-up reporting change passed all 1,355 offline tests, Ruff, formatting
and ty. No further hardware run was used to change the results above.
