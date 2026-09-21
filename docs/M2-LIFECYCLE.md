# M2a lifecycle evidence

This branch adds passive diagnostics and offline replay. It does not establish
YouTube natural-completion support or enable automatic advancement. No new live
hardware result is claimed here.

## What the capture does

`scripts/capture_lifecycle.py capture` opens one explicitly selected Cast receiver
and observes an explicitly selected YouTube ID for a bounded period. It sends
status polls only. It never launches, pauses, resumes, seeks or stops playback.
A second controller may operate the receiver while capture runs.

One `LifecycleTracker` keeps a single domain `SessionSnapshot` across short
observation windows. It uses the existing pure evidence policy instead of resetting
progress through repeated application `status` calls. A fresh matching content and
session sample attaches the scope once. A takeover or connection reset invalidates
it; later callbacks cannot silently reacquire the receiver. A new capture is needed
to examine a new owned session.

The Cast adapter's explicit `read_only` mode refuses effects and keeps at most the
current window's normalized wire records. The collector requests windows no longer
than two seconds, transfers each batch to the journal, and retains only the current
policy snapshot. Ordinary Cast behavior remains compatible. Callback transport I/O
has its own timeouts, so the overall capture deadline is a stop-polling budget rather
than a guarantee that an in-flight network operation can be interrupted instantly.

The journal is JSONL under ignored `runtime/`. It is created exclusively with file
mode `0600`, flushed and fsynced after every complete record. It does not overwrite
an earlier run. An interrupted final write may leave a truncated final line; earlier
lines remain readable. A missing `end` record means capture did not finish cleanly.
A backend failure produces a safe `backend_error` ending without copying exception
text into the report. Ctrl-C ends the capture; it does not stop TV playback.

Each window preserves every **normalized adapter field** available at capture time,
including unknown ads, idle reasons, empty status, connection errors, app transitions
and observation timestamps/order. These are **not original Cast protocol frames**.
The current parser may discard relevant provider fields; a missing signal may require
a separately reviewed adapter change and another capture.

`gap` records distinguish two kinds of observation absence longer than five seconds:

- `transport`: no newer normalized sample from the selected target.
- `media`: no newer matching content/session sample on the attached connection.
  Receiver heartbeats and foreign sessions cannot hide this gap.

`gap_seconds` is the elapsed absence when first noticed, not a fabricated exact
outage duration. Window timestamps show when subsequent samples resumed. Domain
freshness independently expires playback evidence, including during missing data.

A passive attachment always records `entire_run_observed: false` and
`external_control_unobserved: true`. Even when the domain accepts a fresh terminal
signal, the capture alone cannot prove that it observed the entire title or that
another controller did not seek, stop, or change autoplay. Operator notes and the
experiment's launch/control record remain separate evidence.

## Explicit live experiment recipe

Run this only during an explicitly requested hardware session. Confirm the exact
receiver UUID using discovery, leave Sonos on HDMI ARC, and agree on the title and
observation period before starting playback. All private identifiers stay local.

For the already approved Bob Ross episode, start passive capture in one terminal:

```sh
uv run --locked python scripts/capture_lifecycle.py capture \
  --device UUID_FROM_DISCOVER \
  --content FozIp7Va7dY \
  --seconds 2400 \
  --output runtime/lifecycle/bob-ross-run-1.jsonl
```

Start from an idle receiver and wait for the script's `capturing: true` message on
stderr, then use the separately authorized playback controller in another terminal.
Starting capture while a previous copy of the same title is playing can attach that
old session; a subsequent restart will correctly invalidate that scope. The existing
M1 queue still supports only
the fixed Bob Ross example; this capture script accepts another exact YouTube ID
but does not add a general launch command. A second title must have a reviewed,
authorized launch path before counting it toward acceptance.

The 2400-second value is a capture budget with room for the episode and interruptions;
it is not a completion deadline. Increase the budget, up to four hours, if the actual
run requires it. A deadline ending with unknown or playing state remains incomplete.
Keep the controller host awake. Record the actual launch time, visible start, deliberate
pause/resume actions, any seek, ads, buffering, manual intervention, visible ending,
and provider autoplay in separate private operator notes. Do not infer these from a
successful command or from the published runtime.

## Export and replay without hardware

```sh
uv run --locked python scripts/capture_lifecycle.py sanitize \
  runtime/lifecycle/bob-ross-run-1.jsonl \
  runtime/lifecycle/bob-ross-run-1-sanitized.jsonl

uv run --locked python scripts/capture_lifecycle.py replay \
  runtime/lifecycle/bob-ross-run-1-sanitized.jsonl \
  runtime/lifecycle/bob-ross-run-1-replay.jsonl
```

Export allowlists scalar fields and known enum values, replaces device/session/app/
content/capture identities with consistent per-export pseudonyms, removes titles,
addresses, URLs and arbitrary errors, and shifts wall/monotonic timestamps to a common
origin while preserving relative timing. Bare YouTube IDs and recognized watch/short
URLs map to the same content pseudonym. Unknown URL identities are redacted. Export
keys are ephemeral; separate exports cannot be joined by their pseudonyms.

Review the resulting fixture before committing it. `live` becomes `sanitized-live`;
`synthetic` remains `synthetic`. A replay never changes synthetic evidence into live
evidence. Export does not trust saved evidence flags; replay recomputes them through
the Cast adapter and domain policy using the original observation cadence, without
sockets or sleeping. Replaying a truncated/invalid record fails explicitly. Preserve
the original journal and document any removed incomplete final line in a separate
copy; do not present a recovered partial capture as complete.

Normal tests exercise these paths with synthetic inputs: progress then explicit
finished, duplicate terminal events, ads extending past nominal duration, unknown ads,
long pause, buffering, manual cancellation/error, app exit, provider autoplay/takeover,
disconnect, silent timeout, interrupted capture, privacy export and normalized replay.
These tests prove implementation behavior; they are not hardware acceptance runs.

## Acceptance gate

Before enabling automatic advancement, record three natural endings across at least
two real YouTube titles; at least one run includes a deliberate pause and resume.
Seeking near the end may diagnose protocol behavior but cannot count as a natural
ending. Keep failures and inconclusive attempts in the run ledger. Observe and record
ads, buffering, manual stop, errors, app exit and provider autoplay; label any synthetic
ad scenario when no live ad was seen. Record timestamps, observation gaps, software
commit/library versions, requested ID, and separate visual confirmation.

The current policy requires explicit inactive-ad evidence for strict playback and
completion. The current YouTube normalization commonly reports `ad_break: null`.
That remains unknown. An absent optional `breakStatus` field must not be silently
converted to `false` merely to pass this gate.

Cast's protocol distinguishes `FINISHED`, `CANCELLED`, `INTERRUPTED` and `ERROR`;
`IDLE` may also describe an initial state without a reason. Only a correlated,
fresh, evidenced natural ending can authorize advancement. App exit, missing metadata,
reaching duration and the capture deadline cannot substitute for it. See Google's
[media message protocol](https://developers.google.com/cast/docs/media/messages) and
[receiver MediaStatus reference](https://developers.google.com/cast/docs/reference/web_receiver/cast.framework.messages.MediaStatus).

M2b's three three-item queue sessions, six correct handoffs, responsive controls and
restart recovery remain separate later gates in [the roadmap](ROADMAP.md). The passive
collector does not own a queue or persist command intent.
