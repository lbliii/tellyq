# Native YouTube queue prototype checkpoint

This is the historical prototype record. The subsequent foreground integration
and merged hardware results are in [NATIVE-RUNNER-CHECKPOINT.md](NATIVE-RUNNER-CHECKPOINT.md).

The supervised prototype observed two A-to-B receiver transitions after
`play_next(B)`, including one clean repetition without queue readback. A completed
under the existing ordered-history policy; ads followed; then exact B identity
and advancing non-ad positions qualified B playback. This is evidence for a
native YouTube successor experiment. **M2 remains open:** the foreground service
has not integrated or recovery-validated native queue ownership, and the three
three-item sessions with six correct handoffs have not been performed.

## Scope and versions

Four bounded trials ran on 2026-09-21 against one existing Chromecast, with the
controller/evidence code at `39c4ed0`, Python 3.14.0, PyChromecast 14.0.10 and
locked casttube 0.2.1. An isolated private probe called the existing YouTube
controller and observed Cast telemetry. It did not exercise the foreground
runner, durable queue dispatch or restart reconciliation. Original saved
queue/session files were checked unchanged by each probe.

The same two public videos were used: A, [`f9F7yDjSdNA`](https://www.youtube.com/watch?v=f9F7yDjSdNA),
then B, [`hgsIFyITvJE`](https://www.youtube.com/watch?v=hgsIFyITvJE). Each trial
started from observed receiver idle and started A explicitly. Queue staging was
attempted only after qualified A playback. Returned commands and observed
outcomes were journaled separately. Receiver observations cannot prove what the
TV displayed; the visual column below contains only separately supplied answers.

## Trial ledger

| Trial | Attempt and readback | Observed receiver outcome | Separate visual evidence |
| --- | --- | --- | --- |
| 01 — append | `add_to_queue(B)` returned; no queue readback | A playback and natural completion qualified. No B playback during about two minutes of post-terminal observation. Original cleanup refused unknown-ad end state; a separate explicit cleanup was accepted and receiver idle was observed. | User confirmed NASA A playing, then reported that it stayed on an end screen. |
| 02 — insert next | `play_next(B)` returned. Legacy `get_queue_videos()` then raised `HTTPError`; no usable readback. | A completed; nine code-1081 ad observations followed; exact non-ad B progress qualified. Cleanup quit returned and receiver idle was observed. | No separate confirmation of B or cleanup recorded. |
| 03 — cancel staged successor | `play_next(B)` returned; no queue readback. `clear_playlist()` and then `quit_app()` returned before A's completion. | Receiver idle was observed. A bounded 125-second watch saw no B playback or receiver reactivation. | No separate cancellation/cleanup confirmation recorded. |
| 04 — clean insert repetition | `play_next(B)` returned; no queue readback. | A completed; eight code-1081 ad observations followed; exact non-ad B progress qualified. Cleanup quit returned and receiver idle was observed. | No separate confirmation of B or cleanup recorded. |

The positive trials each recorded one initial A start, one B staging call, and a
later cleanup quit. No second `play_video` call or app exit was recorded between
A and B. Their receiver/session identity remained consistent across the
transition, and the receiver reused the same media-session ID for both contents.
Completion-to-B verification included ads and buffering; nominal duration was
never used to infer completion or B playback.

Trial 01 is a failed bounded append attempt, not proof that `add_to_queue` is
universally broken. Trial 02 initially had two changed conditions: insertion
instead of append, plus a failed queue readback. The pinned read path can rebind
on some HTTP failures, so that trial alone cannot isolate the method as the
cause. Trial 04 establishes a successful insertion without that readback; it
does not establish universal reliability or explain the append result.

Trial 03 establishes the combined clear-and-quit outcome over its observation
window. It does **not** independently prove that the remote playlist was cleared:
app exit also prevented playback, and no authoritative queue-clear readback was
obtained. A successful method return remains a command result.

All four trials finished. The last trial's cleanup verified receiver idle; no
prototype owner remains running. This is the checkpoint's final observed state,
not an ongoing claim about the receiver.

The original trial 01 cleanup guard was too strict for its unknown-ad end-screen
state and refused to act. The separate explicit stop succeeded. Later prototype
cleanup allowed unknown ad state only within fresh known owned content and
session, while still refusing a known active ad or takeover. That private probe
adjustment did not relax production playback/completion evidence policy.

## Offline replay evidence

[Sanitized fixtures](../tests/fixtures/native-queue/README.md) preserve selected
complete windows from trials 04 and 02. The clean repetition is primary; trial
02 additionally captures anonymous A FINISHED and the first B identities in the
same returned batch. Every event remains ordered, including missing identities,
unknown states, duration changes and subsequent ads. A fresh replay recomputes
normalization and domain decisions without trusting saved verification flags.

[The replay tests](../tests/test_native_queue_evidence.py) establish these distinct
results under the unchanged evidence policy:

- A's anonymous terminal receives ordered-history attribution from fresh exact A
  identity and qualified prior progress. That historical completion remains the
  same witness through later unknown states, ads and changed content.
- The eight/nine ad samples include B identity and advancing ad positions. They
  do not qualify B playback or completion. Code 5 stays unknown; no numeric code
  is given a new API meaning.
- A separately declared expected B observation scope can qualify B after fresh
  exact non-ad progress. The original A scope still treats B as replacement and
  loses ownership, even though the receiver and media-session IDs match.

These are observation replays. They do not prove durable queue ownership,
exactly-once staging, absence of unobserved external control, production scope
transfer, or recovery. The excerpts omit launch/queue commands and cleanup;
those claims come from the private command journals summarized above. No live
device or network tests run in the normal suite.

The evidence-only change adds 13 replay tests. After `uv sync --locked` using
stable Python 3.14.0, `uv run --locked poe check` passed Ruff, formatting, ty and
all 1,103 tests. The initial sandboxed run passed 1,090 tests and failed 13
existing IPC scenarios because Unix socket binding was denied; the full rerun
with local socket permission passed. The tests continued to block network/DNS.
The first CI packaging check caught the new fixture directory missing from the
source manifest. The manifest now explicitly includes the reviewed native-queue
fixtures; private runtime logs remain excluded.

## Source and implementation boundaries

Google's [Cast queueing guide](https://developers.google.com/cast/docs/web_receiver/queueing)
describes receiver-managed default and custom queues. That general SDK facility
does not establish YouTube's control surface. This experiment used YouTube's
existing MDX/Lounge path through
[PyChromecast 14.0.10's YouTube controller](https://raw.githubusercontent.com/home-assistant-libs/pychromecast/14.0.10/pychromecast/controllers/youtube.py)
and the installed, locked [casttube 0.2.1 package](https://pypi.org/project/casttube/0.2.1/).
Source inspection maps append to `addVideo`, play-next to `insertVideo`, and clear
to `clearPlaylist`. Queue actions may bind before dispatch. The PyChromecast
timeout session may also rebind on certain HTTP errors; readback therefore needs
its own side-effect and freshness review. These are version-specific library
findings, not a stable public Google queue API contract.

[Cast media messages](https://developers.google.com/cast/docs/media/messages)
provide the standard state/terminal context; TellyQ's
[reviewed YouTube completion contract](YOUTUBE-COMPLETION-CONTRACT.md) supplies the
finite provider interpretation and ordered attribution already used here.
Queue command success, missing break metadata, repeated media IDs and duration
alone do not prove playback, inactive ads or ownership.

## Intended continuation and remaining acceptance

Native staging lets the receiver perform the successor transition without a
second local start command. A staged successor can therefore continue while the
local owner is disconnected or down; this is an architectural consequence, not
a crash/reconnect outcome tested in these trials.

The user explicitly selected this intended behavior: allow **one already approved
successor** to continue on the receiver if TellyQ disconnects, then reconcile on
reconnect. It is not yet implemented or recovery-verified. Integration must
durably distinguish approved staging intent, attempted/returned commands and
observed playback; uncertain outcomes must not cause blind restaging or a second
start. Reconciliation must retain historical completion, recognize the approved
successor without adopting arbitrary replacement content, and avoid inventing
completion or progress during the observation gap.

That one-successor approval does not establish the remote playlist's cardinality
or prove that YouTube autoplay is disabled. Bounding local staging to one item
alone cannot prove that the receiver will stop after that item; this needs
separate validation and an explicit supported behavior before unattended claims.

The optional adapter and this evidence checkpoint are preparatory work. Native
queue production wiring, durable successor scope transfer, cancellation and
stop semantics, disconnect/crash recovery and repeat acceptance remain separate
work. The [M2 acceptance gate](M2-ACCEPTANCE.md) still requires three three-item
sessions/six correct handoffs and the specified interruption scenarios. These
two two-item prototype successes do not close that gate.
