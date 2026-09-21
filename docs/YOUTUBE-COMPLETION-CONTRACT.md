# YouTube completion interpretation and attribution

This is a finite adapter contract backed by the pinned first-party implementation
and observations described in [the metadata investigation](YOUTUBE-METADATA.md).
It is not a public stable YouTube enum or a claim that live ad behavior has passed
acceptance. No queue runner or automatic advancement is implemented here.

## Source-qualified states

The source is YouTube remote-player build `4fd832e7`, SHA-256
`08e620a0bd236bf5c0f6e72d36c122fa4340d29a678a91f6889ca1fb5b7f8d43`, specifically
`status.customData.playerState`. The Cast adapter correlates media with a receiver
status received within five seconds and exact application ID `233637DE`, the ID
used by PyChromecast 14.0.10. A display name alone cannot qualify the provider field.

| Provider code | Required standard state | Interpretation |
| --- | --- | --- |
| 0 | IDLE with FINISHED | Ordinary-content terminal candidate |
| 1 | PLAYING, no idle reason | Ordinary content playing |
| 2 | PAUSED, no idle reason | Ordinary content paused |
| 3 | BUFFERING, no idle reason | Ordinary content buffering |
| 1080–1085 | Any | Ad context; never content completion |
| Other, absent, null or malformed | Any | Unknown |

The code must be an integer, not a boolean, string or fractional number. A message
must contain exactly one status and a current media-session identity. Ordinary
states require absent `breakStatus`. A reported break ID or nonnegative elapsed
break time is positive ad evidence and takes precedence over a conflicting provider
code. Empty, null or malformed break objects cannot qualify ordinary content.
Missing break metadata **alone** remains unknown; inactive-ad interpretation comes
from the finite, source-qualified ordinary-content state.

The ordinary/ad distinction follows the reviewed implementation. The initial
provider diagnostic did not capture code 2 or any 108x code. The subsequent
completion checkpoint captured code 2 during a software-verified and user-visible
pause, followed by a verified and visible resume. A later run captured five
code-1081 samples after the requested program ended and different content appeared;
the adapter reported active ad context and retained the original completion.
The user confirmed seeing an ad or another video. Other ad-family codes and
mid-program ad transitions remain unvalidated live. Unknown codes, including
observed -1 and 5, retain unknown meaning. See the
[checkpoint ledger](M2A-CHECKPOINT.md) for commit-specific results.

## Identity and ordered history

Raw observations retain only current-message fields. An anonymous terminal keeps
`content=None`; previously observed content is never copied into it. Each adapter
observation separately describes its identity update and provider provenance.

An anonymous terminal can be attributed through ordered history only when:

- It qualifies as ordinary-content FINISHED under the provider contract above.
- Both primary `media` and `extendedStatus` are genuinely absent. Null, invalid,
  empty objects, and missing/invalid content IDs inside present media do not count
  as omission.
- A recent exact-content sample belongs to the requested content, target,
  connection generation, receiver app/session and the same media-session ID.
- The same source contract has already established advancing PLAYING positions.
- The sequence is ordered and contiguous across all accepted events, including
  receiver status. The exact-content anchor is no older than five seconds at the
  terminal. An initial scope may start at a nonzero sequence.
- No intervening ad, unknown media, source change, backward position, changed media
  session, observation gap, partial replay window, reconnect, replacement or stop
  intent invalidated the chain.

Matching receiver status preserves ownership but does not refresh media identity
or progress. Qualified same-content PAUSED/BUFFERING can preserve the completion
chain while breaking the current PLAYING progress pair. A qualified anonymous
nonterminal can preserve an existing chain briefly, but cannot refresh its last
exact-content anchor. After uncertainty, a new exact-content progress pair is
required; known replacement permanently revokes ownership in that scope.

An explicitly identified terminal still needs confirmed progress, consistent
source/media history and inactive-ad evidence. No duration, proximity-to-duration,
wall-clock timer, command receipt or application launch substitutes for a terminal
witness. The generic domain policy contains no YouTube numeric codes or app IDs.

## Historical completion and current control

A successful decision stores one immutable historical witness with the terminal
sequence/time, identity sequence, source and attribution (`explicit` or
`ordered_history`). Later harmless buffering/idle updates do not erase it or create
a second completion. Stale current telemetry loses current identity/playback
confirmation while retaining the historical witness. A later replacement still
revokes ownership; historical completion grants no permission to control it.
Status preserves a completion followed by replacement in the same returned batch.

The additive JSON `evidence.completion` object labels this witness as `historical`.
For ordered-history completion, current `identity_observed` remains false. The
witness is process-local evidence: snapshot persistence continues to retain durable
identity/history only and does not restore monotonic evidence after a process
restart. Report journals retain the historical result for inspection.

Routine status observation windows are two seconds, within the existing
five-second freshness policy, so an early terminal remains fresh when delivered.
Start retains its requested observation duration; pause/resume and stop retain
six-second verification budgets. A very short title that ends early in a long
start window may therefore remain unconfirmed. This conservative limitation never
turns stale queued data into fresh evidence; continuous lifecycle capture uses
bounded two-second windows.

Sanitized replay retains the public YouTube app ID and reviewed provider code and
shape, pseudonymizes private identities, and recomputes evidence. It never trusts
saved evidence flags or arbitrary custom-data objects. Historical captures without
provider values remain unconfirmed. Replay provenance remains synthetic or
sanitized-live, never new hardware evidence.
