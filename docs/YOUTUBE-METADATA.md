# YouTube duration and completion evidence

Reviewed 2026-09-21 against the locked PyChromecast 14.0.10 and casttube 0.2.1
sources, Google's public Cast/YouTube references and first-party player source,
and supervised diagnostics using the `e4b2a91` controller. Public API descriptions,
library behavior and actual receiver
observations are separate evidence below. No completion-policy change is proposed
from a field name or an undocumented numeric value alone.

## Duration is already available

The repaired live diagnostic received **112.901 or 113.0 seconds** of duration in
109 exact-title observations of NASA's *Why Does NASA Study Earth?* The last
exact-title sample before `FINISHED` reported position 112.777 and duration
112.901. The terminal message omitted both content identity and duration. Those
numbers describe the trace; proximity to the duration did not establish completion.

Google's [MediaInfo reference](https://developers.google.com/cast/docs/reference/web_sender/chrome.cast.media.MediaInfo#duration)
defines duration in seconds, with null permitted for live media. Separately,
YouTube's [video resource](https://developers.google.com/youtube/v3/docs/videos#contentDetails.duration)
provides the video's length as an ISO 8601 value and notes that transcoding can
make it differ slightly from playback duration. That catalog value is useful for
lineup estimates; it is not a report from this TV session.

TellyQ already retains observed duration and position. The missing capability is
a justified decision that the requested program ended. Wall-clock runtime does
not account for ads, pauses or buffering. A final position near duration is useful
corroboration, but cannot replace ownership, identity, ordering and terminal
evidence. A seek or replacement can also put a player near the end.

## What the documented protocol says

| Field | Documented meaning | Consequence for this investigation |
| --- | --- | --- |
| `media.contentId` | Provider-specific content identity | Retain exact content and where it was observed. |
| `media` | Optional incremental media information | Its absence in a terminal update is permitted; distinguish missing from invalid. |
| `idleReason` | Distinguishes FINISHED, CANCELLED, INTERRUPTED and ERROR | FINISHED is a real terminal signal, but must be attributed to the requested content. |
| `mediaSessionId` | Playback instance identity | Preserve its history; observed reuse across content means it cannot be our sole attribution rule. |
| `customData` | Application-defined data | An object, even an empty one, does not imply usable YouTube metadata. |

These are [Cast media message semantics](https://developers.google.com/cast/docs/media/messages),
not a guarantee that every receiver supplies every optional field. In particular,
the media object need not be repeated when unchanged. Treating an omitted terminal
identity as a fresh explicit identity would be wrong; a future correlation rule
would need to label its use of prior observations and invalidate it at uncertain
boundaries.

The [MediaStatus reference](https://developers.google.com/cast/docs/reference/web_receiver/cast.framework.messages.MediaStatus)
also specifies optional `breakStatus`, queue-item IDs, and `extendedStatus` for
additional loading information. [BreakStatus](https://developers.google.com/cast/docs/reference/web_receiver/cast.framework.messages.BreakStatus)
describes current break/clip IDs and elapsed times. These references do not give
YouTube-specific `customData` keys or define missing break metadata as proof that
an ad is inactive. Queue IDs, skip-ad support and a metadata list of possible
breaks are not interchangeable with current ad state.

The [CAF ad-break guide](https://developers.google.com/cast/docs/web_receiver/ad_breaks#default_break_behavior)
does define break-status broadcasts during SDK-managed ads, with `breakStatus`
present only during a break. It also describes different timelines that include
or exclude ad time from content duration. For a validated implementation of that
contract, absence in an appropriate complete status can have a defined meaning.
We have not established that YouTube reports every ad through this mechanism.

Requiring explicit inactive-ad evidence is TellyQ's current conservative policy;
it is not a universal Cast requirement for a literal false boolean. A documented,
source-specific rule could resolve uncertainty without such a boolean, provided
the route's contract and observed behavior justify it. That would require a
separate policy review and live acceptance, including ads and content transitions.
This batch neither declares missing ads inactive nor changes the completion gate.

No public YouTube Cast custom-data schema was found in this bounded primary-source
review. A plausible key such as `isAd` is therefore only a diagnostic candidate
until its actual source, type and semantics are established. An undocumented
numeric player code must not inherit the IFrame API's meanings merely because
the numbers look familiar.

## A concrete provider-state lead

The fresh schema capture found one numeric field at
`status.customData.playerState`, and `currentIndex` (number) plus `listId` (string)
inside media custom data. No additional fields were present in that run. Schema
capture retained names/types rather than values; the playlist identifier is not
needed and stays private.

YouTube's own public client implementation gives this specific field a stronger
basis for investigation. On 2026-09-21, its [IFrame loader](https://www.youtube.com/iframe_api)
referenced player build `4fd832e7`. The corresponding
[first-party remote-player script](https://www.youtube.com/s/player/4fd832e7/player_ias.vflset/en_US/remote.js)
was inspected with SHA-256
`08e620a0bd236bf5c0f6e72d36c122fa4340d29a678a91f6889ca1fb5b7f8d43`.
The full downloaded source stays in ignored local research files.

Three distinct implementation facts matter:

1. Its Cast update function (`Yd$` in this build) directly reads the Cast media
   object's `customData.playerState` into the remote player context, alongside
   media content ID. This is the same provider field found in the live schema.
2. A separate MDX `onAdStateChange` handler (`O85`) maps incoming ad-state values
   1, 2 and 0 to player-state codes 1081, 1084 and 1083 respectively, and other
   values to 1085. This is an ad-specific code family, not an assumption based on
   the IFrame API's public numeric states.
3. The remote UI groups those codes with playing, paused, ended and buffering
   presentations. Its `getAdState()` method returns an internal bucket of 1 for
   1080/1081/1084/1085, 0 for 1082/1083, and -1 otherwise. Ordinary context helpers
   separately identify state 1 as playing and 3 as buffering.

The last point supports a distinction between ordinary and ad-specific states,
but the default bucket also includes unknown values. It does not justify turning
every code outside 1081, or every absent/invalid value, into inactive-ad evidence.
The source does not provide a stable public enum contract or prove what this
receiver sends during actual ads. Keep exact code, source and observation together;
do not collapse them into an ad boolean in this diagnostic batch.

An independent implementation also consumes the same path:
[CastBlock's parser](https://github.com/erdnaxeli/castblock/blob/9f9b57cc55d090eb56934664c7454881d778cbcb/src/chromecast/watch_message.cr)
reads an integer, and its [control code](https://github.com/erdnaxeli/castblock/blob/9f9b57cc55d090eb56934664c7454881d778cbcb/src/blocker.cr)
treats 1081 as an ad. That pinned revision dates to 2023-07-07. It is corroborating
project-author code, not a Google guarantee or validation on this TV. Its shortcut
that any other value means an ad ended is deliberately not adopted.

## What the pinned libraries actually do

[PyChromecast 14.0.10's `MediaStatus.update`](https://github.com/home-assistant-libs/pychromecast/blob/14.0.10/pychromecast/controllers/media.py)
keeps previous content ID, duration, position and media custom data when an update
omits those fields. It falls back to `extendedStatus.media` when primary media is
empty. Its stored object is consequently a useful current-state cache, not proof
that each field arrived in the latest message. It does not retain status-level
custom data through that property path. TellyQ instead normalizes incoming frames
directly and records field provenance; this distinction must survive any future
correlation work.

[The pinned YouTube controller](https://raw.githubusercontent.com/home-assistant-libs/pychromecast/14.0.10/pychromecast/controllers/youtube.py)
uses the YouTube MDX namespace to obtain a screen ID and casttube to launch/manage
the queue. The implemented MDX status handler extracts that screen ID; it is not
a content/ad/completion observer. Obtaining a screen ID can launch the app, so it
must not be added to an allegedly passive preflight.

The installed casttube 0.2.1 source includes `get_session_data()`, which polls the
Lounge bind endpoint, and `get_queue_playlist_id()`, which reads `listId` from a
`nowPlaying` entry. See the [upstream implementation](https://github.com/ur1katz/casttube/blob/master/casttube/YouTubeSession.py)
and [versioned package](https://pypi.org/project/casttube/0.2.1/). The inspected
package provides no typed content/ad/completion contract. Its request machinery
also holds pairing/session tokens and may rebind after errors. A Lounge probe is
an implementation-based research option, not a documented Google observation API
or a proven replacement for Cast telemetry.

For a bounded probe, review the installed method's fixed `AID=5`, response ordering
and freshness explicitly. The PyChromecast timeout subclass also attempts a rebind
on certain HTTP errors. Polling must not silently recover into a different scope;
an unknown/error result should end this experiment. These are source-inspection
findings, not observed failures of a live Lounge probe.

## What is proven on this receiver

The first merged checkpoint (`7a08929`) recorded three requested-title terminal
candidates across two titles, all with unknown ads and omitted terminal identity.
One capture then showed different content using the same media-session ID. That
is stronger evidence against identity-only attribution than a generic uniqueness
description is evidence for it. The cause of subsequent playback was not proven.

The repaired diagnostic (`e4b2a91`) passed guarded pause/resume and cleanup stop.
The user separately confirmed seeing the pause. Resume, progress after resume and
cleanup were verified through telemetry; visible resume, ending and cleanup were
not separately confirmed. It recorded one more anonymous FINISHED candidate.

In all 119 media observations, status custom data had an object shape. Media custom
data had an object shape in the 109 playback observations and was unavailable in
the other ten. The prior capture retained shapes only. It cannot tell us whether
those objects were empty, which keys existed, or what values they carried. This
motivated the scoped metadata capture below.

The first schema run used observer `61b839e` and the unchanged `e4b2a91` controller.
It found nonempty provider data: numeric status `playerState` and media
`currentIndex`/`listId` fields. The follow-up observer at `3a60058` then retained
only the reviewed numeric `playerState` value, without interpreting it or keeping
playlist IDs. Its controller also remained on `e4b2a91`.

| Captured provider code | Associated standard receiver observation |
| --- | --- |
| -1 | Startup, including one exact-title BUFFERING sample |
| 3 | BUFFERING |
| 1 | PLAYING, usually with the requested title; one update omitted identity |
| 0 | IDLE / FINISHED, omitted identity and duration, same immediately preceding media-session ID |
| 5 | Subsequent BUFFERING with the requested title and position zero, using the same media-session ID |

The terminal position was 112.901 seconds, following an identified sample at
111.579. Neither proximity to duration nor the numeric zero closed acceptance.
No 108x ad-specific code was observed; that does not establish absence of ads or
validate how this receiver reports them. The inspected first-party source did not
map code 5, so it stays opaque instead of inheriting an IFrame interpretation.
The [checkpoint ledger](M2A-CHECKPOINT.md) records capture integrity and cleanup.

## Decision and bounded next work

The metadata capture must preserve source paths, empty versus nonempty containers,
and only reviewed fields or privacy-safe summaries. Arbitrary payloads, pairing
material, receiver/session IDs and local addresses stay out of Git. A parser's
allowlist is a privacy boundary, not a declaration that a provider supports those
fields. The experiment uses a finite observation budget, not an end-time guess.

The result is a **concrete provider-state candidate**, not a proven completion
capability. Duration was never the missing field. The next batch should review a
finite YouTube-specific interpretation and ordered content correlation, using the
pinned first-party source and captured codes together. Keep unknown codes,
malformed values, conflicting standard/provider states, ads, content replacement,
reconnects and observation gaps explicit. Previously cached content must not become
freshly observed identity, and an anonymous terminal needs its own documented
attribution rule. The present production gate remains unchanged.

Once that rule has a defensible contract and offline replay coverage, repeat the
three-run/two-title acceptance set, preserving start/control chronology and visible
evidence separately. Validate ad-specific behavior or retain its exact untested
limitation; ordinary-code runs alone do not validate the positive ad path. Do not
repeat schema-only captures now that the useful field has been identified.

If the candidate cannot support reliable completion, record that limitation. The
smallest next research option is one bounded observation through the already
established casttube session's `get_session_data()`, with tokens kept private,
no queue mutation, and no automatic rebinding/relaunch disguised as observation.
Failure, unsupported semantics or insufficient fields ends that probe; the queue
remains assisted and held for attention.

The official [YouTube IFrame API](https://developers.google.com/youtube/iframe_api_reference)
exposes duration and player events for an embedded player, but does not attach to
this existing Chromecast playback session. Replacing the playback surface would
be a separate architecture experiment, not a metadata fix. No new UI, hardware
or custom receiver is introduced here.

M2a still requires three supported natural endings across two titles and a
justified completion decision. The repaired pause/resume gate has passed. The
runner, automatic queue advancement and cross-service work remain subsequent
milestones; see the [checkpoint ledger](M2A-CHECKPOINT.md).
