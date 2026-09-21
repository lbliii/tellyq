# Acknowledged enqueue investigation

Both bounded comparisons passed: A → B → A and A → B → C each produced three
verified natural endings and two automatic handoffs. This establishes that both
repeated and distinct third videos can work. It does **not** reproduce or fix the
earlier intermittent stall. **M2 remains open**; these two instrumented comparisons
do not replace the full acceptance batch or erase its failed attempt.

| Trial | Observed outcome | Final verified stop | Driver / owner exit |
| --- | --- | --- | --- |
| Repeated A → B → A | All three items finished; two exact advancing native successors | 2.398 seconds; local acknowledgement 0.747 ms | 0 / 0 |
| Distinct A → B → C | All three items finished; two exact advancing native successors | 2.330 seconds; local acknowledgement 0.864 ms | 0 / 0 |

Each durable history contains one initial START, two acknowledged PLAY_NEXT
operations and one final STOP. There were no retries, fallback starts, CLEAR,
pause or resume commands. Initial playback verification took 38.334 and 37.881
seconds respectively. The repaired checkpoint driver correctly returned success
for `queue_finished` with separately verified cleanup.

The user confirmed visible advancement and audible nature sound in the distinct
trial. This confirms sound on that run; it does not retrospectively establish
audio during the earlier quiet checkpoint. Receiver cleanup was verified for
both runs and both owners exited. The first trial has no separate human playback
confirmation. No positive ad-family state or Cast break was captured in either
comparison; that is not a claim that these videos are always ad-free.

## Scope and method

The 2026-09-21 investigation follows the stalled second session in
[NATIVE-RUNNER-CHECKPOINT.md](NATIVE-RUNNER-CHECKPOINT.md). It compares repeated
A → B → A with distinct A → B → C on the same selected Chromecast. A and B are
the existing quiet RSPB clips; C is RSPB's Inversnaid, verified through YouTube's
public oEmbed metadata before selection. Each trial has a fresh queue and owner
runtime; the preceding owner closes before the next begins.

Tested playback code is merged `b0e32b1`, with Python 3.14.0, PyChromecast 14.0.10
and casttube 0.2.1. All 1,355 offline tests, lint, formatting and ty passed before
the runs. The foreground service, IPC checkpoint driver and playback rules are
unchanged. Private process-local wrappers record the responses from the library's
existing POST calls and TellyQ's ordered typed receiver records. They do not add
HTTP calls, playlist readback, retries or playback commands. Logging itself adds
I/O, so these are instrumented runs, not a claim of identical execution timing.

Each trial has a 420-second observation budget followed by the existing guarded
cleanup, with a separate 25-second verification budget. Those deadlines bound the
experiment; they never establish content completion. Private manifests, journals,
instrumentation source hashes and owner logs remain under ignored `runtime/`.
HTTP captures exclude headers, tokens, raw response bodies and session identifiers.
The distinct trial additionally records ephemeral keyed fingerprints of session
and playlist identifiers, response integer triples, event field names and selected
autoplay/shuffle fields. This changes diagnostic output only.

## What the response capture establishes

The installed [casttube `play_next` route](https://github.com/ur1katz/casttube/blob/master/casttube/YouTubeSession.py)
rebinds, then sends `insertVideo`. PyChromecast's
[`_do_post` implementation](https://github.com/home-assistant-libs/pychromecast/blob/master/pychromecast/controllers/youtube.py)
checks HTTP status; the queue method discards the response. A successful return
does not verify insertion. In the capture, mutation responses are ten-byte JSON
arrays with three integers and no named queue event. The distinct run records
`[0, -1, 0]`. This document assigns no application-level success meaning to those
undocumented numbers.

Bind responses contain named events, including `playlistModified` and
`onAutoplayModeChanged`. Before each enqueue, `playlistModified.videoId` matches
the current requested clip. These snapshots precede the insertion and contain no
complete ordered membership list. Neither that event's name nor its current-video
field proves that the next clip was inserted.

In the distinct trial, the initial launch and two inserts use different session
fingerprints; the playlist fingerprint remains stable. That observation does not
support a same-session request-counter collision in this particular run. The
earlier failed trial did not capture this information, so it cannot rule that
hypothesis out retrospectively.

The initial bind reports autoplay `ENABLED`; the two binds during explicit
playback report `UNSUPPORTED`. These are phase-specific provider reports, not
proof that recommendation autoplay is disabled or that a remote queue contains
exactly one successor. No autoplay, loop, shuffle, volume or mute setting was
changed by the investigation.

## Existing implementations and limits

- [casttube PR 10](https://github.com/ur1katz/casttube/pull/10) proposes parsing
  multiple length-prefixed JSON chunks. Its current `get_session_data` instead
  flattens the response before decoding it.
- [casttube PR 11](https://github.com/ur1katz/casttube/pull/11), still open when
  checked, proposes retaining messages from bind and command responses. Its author
  also reports that the old playlist-fetching endpoint still does not work. This
  is a useful observability lead, not a demonstrated fix for the missed handoff.
- [pyytlounge's wrapper](https://github.com/FabioGNR/pyytlounge/blob/master/src/pyytlounge/wrapper.py)
  already implements a subscription using the latest event cursor and dispatches
  playback, ad, autoplay and next-recommendation events. Its
  [event types](https://github.com/FabioGNR/pyytlounge/blob/master/src/pyytlounge/events.py)
  provide examples to evaluate before introducing another protocol client.
  It has not been installed or tested with TellyQ. Its current subscription has an
  unbounded total timeout and logs pairing/session material, so it is not a drop-in
  fit for this controller's bounded cancellation and private diagnostics.
- [ytcast's queue implementation](https://github.com/MarcoLucidi01/ytcast/blob/master/youtube/remote.go)
  uses separate request indices and randomized two-to-five-second delays between
  `addVideo` calls, with a comment about lost entries. That is a different command
  and request pattern. TellyQ's inserts occur roughly a clip apart and rebind each
  time; copying that delay would not be an evidence-based fix for this failure.

These are community implementations, not an official Google Python SDK or a
documented delivery guarantee. Reading their code does not establish compatibility
with this receiver. No dependency or transport behavior changes were made here.

## Next discriminating experiment

Capture the provider's post-insertion event stream alongside Cast evidence within
one foreground owner. First evaluate the existing clients above for bounded
timeouts, cursor handling, session ownership, redaction and Python 3.14 support.
Prefer reusing an established reader; do not install a second independent TV
controller beside the current owner or revive the broken playlist HTML endpoint.

The useful distinction is: did a scoped post-request event establish the approved
successor's presence, or did only the HTTP request return? If membership is never
observable, preserve that limitation explicitly. A next recommendation is not an
approved reservation, and any new telemetry remains diagnostic until its identity,
ordering and freshness rules have replay coverage. On a stall, keep the original
command, response and subsequent event sequence; do not resend or use another
START to conceal the failure.
