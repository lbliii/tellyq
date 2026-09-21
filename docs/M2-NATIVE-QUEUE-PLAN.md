# M2 native YouTube queue integration

Historical planning baseline: `39c4ed0`, 2026-09-21. The isolated hardware experiment
demonstrated an alternative to exiting YouTube between programs. Implementation
and subsequent evidence are now summarized in the [M2 closeout](M2-ACCEPTANCE.md):
M2 is closed with the intermittent enqueue stall retained as an open defect. The
plan below preserves the original scope and is not a new list of pending work.

## Evidence and selected route

The existing PyChromecast 14.0.10 `YouTubeController.play_next(video_id)` requests
insertion after the current video. It does not mean immediate skip. Two supervised
A → B experiments verified A's natural completion, intervening YouTube ad-state
observations, and exact B playback with advancing position. Neither issued an app
exit or second `play_video` between A and B. The second success used no queue
readback. A separate cancellation experiment issued `clear_playlist` and app exit;
the receiver stayed idle and B was not observed during 125 seconds of monitoring.

One `add_to_queue` attempt returned without a verified successor. That single
failure does not establish universal lack of support. An attempted library queue
readback failed with an HTTP error; it is not part of the selected control route.
Successful mutation calls are command outcomes, not membership or playback proof.
The cancellation experiment does not independently establish that the remote queue
was emptied rather than made inactive by app exit.

Private run records remain in ignored `runtime/checkpoints/`. The separate native
queue checkpoint document and sanitized replay fixtures preserve the detailed
evidence and visual-confirmation limits. Use existing library operations; do not
create a replacement Lounge protocol client or infer public API guarantees from
YouTube implementation details.

## Approved behavior

The user explicitly approved **one already authorized successor continuing on the
receiver after TellyQ disconnects, followed by reconciliation on reconnect**.

This changes the old rule that missing local completion evidence necessarily holds
all remote advancement. Once a successor is staged, the receiver may begin it
without another controller decision. A local hold prevents further staging; it
does not retract work already sent to YouTube. Reports must make that distinction.

Only one successor may be outstanding. Its authorization identifies the local
queue and generation, predecessor attempt, successor item, exact content, target,
and the receiver session in which it was staged. This is durable intent, not
restored live authority. Start, enqueue, clear, app exit, observed playback, and
historical completion remain distinct facts.

The one-successor bound limits TellyQ's reservations. It does not prove that the
remote playlist contains only that item or disable YouTube's own autoplay.
Behavior after the final approved item while the controller is absent remains a
separate validation limit; unexpected content is never adopted as approved work.

## First batch: independently reviewable foundations

All branches start directly from the baseline and target `main`.

1. **Optional provider capability.** Add typed native-queue request/receipt values
   and a structural backend protocol. The Cast adapter forwards guarded requests
   to the initialized YouTube controller's `play_next` and `clear_playlist`.
   Missing capabilities reject rather than pretend success. Keep the foreground
   runner unchanged in this batch. No implicit app launch, queue readback, or
   automatic retry is allowed. Upstream rebinding and transport races remain
   explicit limitations; a thin adapter cannot promise an atomic remote scope
   check or exactly-once delivery.
2. **Replay and checkpoint evidence.** Preserve sanitized actual chronology and
   test qualified A completion, ad observations, and separately verified B
   playback. Preserve completion when B arrives in the same observation batch.
   Existing ownership rules must continue rejecting unrelated content; an
   expected successor requires its own declared scope. Record failed attempts
   and separate human observations from receiver evidence.
3. **Integration and recovery plan.** Record the approved continuation behavior,
   storage changes, reconciliation boundaries, and the acceptance gates below.

## Foreground integration

The capability, replay foundations, durable reservations, passive observation and
opt-in [foreground native runner](NATIVE-QUEUE-RUNNER.md) have landed. The sections
below preserve their implementation requirements; current aggregate acceptance
and remaining limits are in the closeout record.

### Durable reservation and dispatch

- Persist a reservation and operation identity before enqueueing. Record dispatch
  before the remote effect, then its accepted, rejected, or unknown outcome.
  A pending reservation does not make B the currently playing item.
- Keep at most one outstanding successor per queue/session. An uncertain enqueue
  may have taken effect: suppress both another enqueue and the old RELEASE/START
  fallback until reconciliation resolves it. Never use a command return to mark
  A finished or B playing.
- Extend attempt provenance so B can originate from a native reservation. The
  existing store and queue validator require one START per attempt; change those
  invariants explicitly rather than fabricating a START receipt for B.
- Use separate reservation/operation records and a deliberate schema migration.
  The current store validates exact table SQL, so adding tables silently to its
  existing schema is invalid. Preserve old queues and histories through an
  exact-known-version migration; keep native mode explicit and legacy behavior
  unchanged for existing queues.
- Initially decline adjacent repeated content IDs (A → A). Reused media-session
  IDs and position resets do not prove which occurrence is playing. A → B → A
  remains distinguishable with a single outstanding reservation and ordered
  observations.

### Observing the transition

- Process every event in order, including A's terminal and B's initial observation
  arriving in the same batch. Retain A's qualified completion as historical fact.
- Recognize B only against the specific outstanding authorization and fresh target,
  connection, app/session, and content observations. Establishing a control scope
  is separate from proving non-ad playback and progress.
- If B starts without observed A completion, do not invent the missing finish.
  Preserve the uncertain predecessor outcome and block further staging while the
  already approved B may continue. Unexpected content or a changed session is not
  a successful planned transition.
- Stage C only after reconciling the A → B reservation and meeting the evidence
  requirements for the new current item. Keep generic release/idle behavior for
  other routes without imposing it on native YouTube transitions.

### Stop and reconnect

- Persist cancellation before further device effects. Account for an already
  staged successor, including an unknown enqueue outcome. A failed or uncertain
  clear cannot be reported as an empty remote queue. Confirm app exit separately;
  preserve uncertain cleanup when current ownership cannot be established.
- Separate a recovery/reconciliation hold from user cancellation. Reopening for
  status must observe without enqueueing, relaunching, clearing, or retrying.
- Fresh observations may reconcile already-running authorized B after reconnect.
  Durable reservation history supplies intent, never fresh session ownership,
  completion, or a reusable monotonic timestamp. Do not dispatch C as an incidental
  effect of status-only recovery.
- Keep the existing healthy-LAN stop targets: local acknowledgement within one
  second, observed outcome within ten seconds. Record failures and uncertain
  remote outcomes honestly.

## Acceptance gates

Offline tests cover intent/dispatch/receipt crash boundaries, unknown enqueue and
clear outcomes, no replay or fallback duplication, cancellation races, same-batch
terminal/transition events, ads, missing terminal evidence, stale/reordered events,
takeover, repeated IDs, and status-only reconnect. Tests must distinguish local
effect dispatch from receiver-autonomous advancement.

After the composed runner passes those checks, repeat the M2 hardware gate:
three three-item sessions yielding six verified handoffs, using A → B → A where
appropriate; active stop with a staged successor; and bounded disconnect/reconnect
checks. Verify the user-approved continuation behavior without duplicate starts,
false completion, or implicit commands on reopen. Preserve all attempts and report
any missing visual confirmations separately.

The two successful prototype handoffs establish feasibility. They do not replace
the composed-runner acceptance gate, prove all ad variants, establish queue
membership readback, or validate crash recovery.
