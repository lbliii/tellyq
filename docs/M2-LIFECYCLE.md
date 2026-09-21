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
Already queued normalized callbacks are drained after a transport interruption and
journaled as a `partial: true` window. These diagnostic tails do not update live
policy evidence, and offline replay also excludes them from completion decisions.

Each window preserves every **normalized adapter field** available at capture time,
including unknown ads, idle reasons, empty status, connection errors, app transitions
and observation timestamps/order. These are **not original Cast protocol frames**.
The parser intentionally discards arbitrary provider payloads. New media records
also describe a fixed set of wire field shapes, so another capture can distinguish
omission from invalid fields or discarded alternatives without saving the payloads.
Older captures cannot reconstruct this distinction from normalized nulls.

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

uv run --locked python scripts/capture_lifecycle.py inspect \
  runtime/lifecycle/bob-ross-run-1.jsonl
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
An additional [reviewed observed excerpt](../tests/fixtures/lifecycle/README.md)
preserves the A3 anonymous endings and intervening content change. It is explicitly
incomplete. Separate, labelled synthetic mutations establish that stronger ad or
identity claims still cannot reclaim a scope after a known takeover.

## Completion blocker and field provenance

The merged-main checkpoint at `7a08929` produced three requested-title terminal
candidates, all with unknown ads and missing terminal content identity. A3 then
reported different content using the same media-session identifier and another
FINISHED. Strict completion remains blocked; this change does not enable
advancement or carry content identity forward onto anonymous terminals.

TellyQ's `Observer.receive_message` sees the incoming media JSON and applies its
own normalizer. It does not source these observations from PyChromecast's mutable
`MediaStatus` cache. That distinction matters: the pinned library carries prior
content, position and other fields forward when updates omit them. Cached identity
would therefore not establish that a terminal message supplied fresh content.
See [PyChromecast 14.0.10 MediaStatus.update](https://github.com/home-assistant-libs/pychromecast/blob/14.0.10/pychromecast/controllers/media.py).

Google's [media protocol](https://developers.google.com/cast/docs/media/messages)
allows status updates to omit unchanged media information and distinguishes natural
FINISHED from cancellation, interruption and error. Omission alone is therefore
not malformed. However, the observed same-ID content change rules out attributing
an ending from media-session identity alone. Ordered content history with reviewed
boundaries might support future attribution; these traces do not establish whether
that would be sufficient. Missing terminal identity and unknown ads still block
the current strict completion policy.

The [MediaStatus reference](https://developers.google.com/cast/docs/reference/web_receiver/cast.framework.messages.MediaStatus)
defines optional `breakStatus`, application-specific `customData`, extended loading
status and queue item identifiers. It does not establish that missing break status
proves an ad-free YouTube stream. The current normalizer can detect reported break
activity, but has no validated source of explicit inactive-ad evidence on this route.
Neither absent fields, empty objects, metadata break lists nor control support flags
are promoted into `ad_break: false`.

New `Observation` fields are additive diagnostics only:

| Diagnostic | Incoming field examined |
| --- | --- |
| `wire_status_count` | Number of entries in `status`, or null for a non-list |
| `wire_media`, `wire_media_content_id` | First status's `media` and its `contentId` |
| `wire_extended_status`, `wire_extended_media`, `wire_extended_content_id` | `extendedStatus`, its `media`, and that media's `contentId` |
| `wire_break_status` | `breakStatus` object |
| `wire_break_id`, `wire_break_clip_id`, `wire_break_time`, `wire_break_clip_time` | Its `breakId`, `breakClipId`, `currentBreakTime`, `currentBreakClipTime` |
| `wire_status_custom_data`, `wire_media_custom_data`, `wire_extended_media_custom_data` | Presence/type of the three known `customData` containers |
| `wire_media_breaks`, `wire_media_break_clips` | Primary media's `breaks` and `breakClips` lists |
| `wire_current_item_id`, `wire_loading_item_id`, `wire_preloaded_item_id` | Nonnegative integer queue-item identifiers; their values are discarded |

Shapes are `absent` (valid parent, missing key), `null`, `valid`, `invalid`, or
`unavailable` (parent missing, null or invalid). `valid` describes type/shape only:
an empty object/list is still a valid container, not complete or truthful metadata.
Nonempty strings, finite nonnegative seconds and nonnegative integer IDs are
validated without coercion. No arbitrary keys, custom data, break IDs, URLs or
queue-item values cross this diagnostic boundary. Fields always describe the
current message. Explicit empty media status retains its legacy two-field shape.
The first status remains the sole normalization input; count >1 exposes that
limitation without silently selecting a different media session.

`inspect` validates and sanitizes input in memory, then prints only aggregate
media/terminal counts and wire-shape histograms. Requested/other/unknown terminal
identity comes from each terminal's own content field; no media-session inference
is used. It reports partial windows, partial terminal counts, end-record presence
and stop reason separately. It never marks a capture accepted or advances anything.
Old traces correctly report zero wire-diagnostic coverage. Replay still recomputes
policy independently and excludes interrupted partial windows.

## Next bounded diagnostic

During the next explicitly authorized hardware checkpoint, use the same passive
capture command on a fresh short title, starting from a visibly idle receiver before
launch. Preserve the independent launch/control log, actual user observations and
the natural ending plus subsequent app/content state. Do not seek. Use a finite
budget with headroom for pause, buffering and ads; deadline is still not completion.

Inspect the resulting trace before repeating three full acceptance runs:

1. Check wire-diagnostic coverage and compare all-message with terminal histograms.
   If primary/extended content or break fields are malformed or a valid alternative
   is unselected, inspect that named normalization path with a synthetic test.
2. If only provider `customData` or queue-item fields are present, review a separately
   scoped field parser or provider observation route first. Shape availability does
   not establish its semantics. Do not export arbitrary provider payloads or tokens.
3. If no supported path supplies fresh content attribution and explicit inactive-ad
   evidence, record the current Cast route as insufficient for unattended completion.
   Keep assisted start/status/stop available; do not repeat identical runs hoping an
   anonymous FINISHED or the nominal duration will pass.

Only after a supported evidence route exists should the full natural-ending gate
below be rerun. Pause/resume repair and its live verification remain a parallel gate.

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
