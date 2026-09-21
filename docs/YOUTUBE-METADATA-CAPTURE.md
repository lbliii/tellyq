# YouTube metadata diagnostic

This opt-in tool inspects the **current incoming Cast media message**. It does not
launch, pause, resume, seek, stop, infer natural completion or advance a queue.
Ordinary TellyQ reports and domain evidence are unchanged. It does not read the
library's cached `MediaStatus` fields.

Use only during an explicitly authorized hardware diagnostic, with the exact
selected receiver and title. Keep the independent playback controller and operator
notes separate. Start this observer before launching the title:

```sh
uv run --locked python scripts/capture_youtube_metadata.py \
  --device UUID_FROM_DISCOVER --content EXACT_11_CHARACTER_VIDEO_ID \
  --seconds 240 --output runtime/youtube-metadata/run-1.jsonl \
  --private-schema-output runtime/youtube-metadata/run-1-private-schema.jsonl
```

The script emits `{"capturing": true, "read_only": true}` on stderr after the
read-only connection and journal are ready. Then the separately authorized
controller can launch playback. There is no implicit discovery choice, playback
effect, automatic retry or completion deadline. The finite budget is at most four
hours; an in-flight transport operation retains its own timeout. Ctrl-C ends this
observer without stopping playback. Do not seek during a natural-ending experiment.

The normal journal contains fixed-key projections of the three known `customData`
containers: first status, its `media`, and its `extendedStatus.media`. It distinguishes
absent, null, wrong type, empty object and nonempty object. Type histograms, inspected
node counts, maximum depth and truncation reveal whether any structured data exists.
Traversal is limited to 128 nodes and six levels per container, and container entry
counts are capped at 10,000. Arbitrary provider key names and scalar values are never
included in this structural projection. Booleans such as a hypothetical `isAd`
are not promoted to evidence merely because they exist.

The only reviewed provider scalar exception is the first status's exact
`customData.playerState` path. The fixed `custom_player_state` field retains a
signed 32-bit integer, with no coercion of booleans, floats or strings. Unknown
in-range codes remain observable as diagnostics. `custom_player_state_shape`
distinguishes valid, absent, null, invalid and unavailable-parent cases. No prior
code is carried into a terminal message that omits it, and values at similarly
named nested/media paths are not selected. `listId`, `currentIndex` and every other
custom-data scalar remain excluded.

This path is used by the independent [CastBlock receiver-message parser at `9f9b57c`](https://github.com/erdnaxeli/castblock/blob/9f9b57cc55d090eb56934664c7454881d778cbcb/src/chromecast/watch_message.cr),
which is primary implementation evidence for inspecting an integer, **not an official
YouTube protocol contract**. This tool does not translate any code into playback,
ad, title or completion evidence. In particular, a reported code cannot establish
inactive ads or authorize advancement without separately validated semantics.

Each sample also reports standard current-message position, duration, player state,
idle reason and the existing ad normalization, plus whether current content matches
the requested title. Missing fields stay null/unknown. `media_session_relation`
compares only the current and immediately previous media message: first, same,
changed or unknown. Missing IDs clear the comparison. Session equality does not
attribute an anonymous terminal to a title, and same-session content changes remain
possible. Only the first status is examined; `status_count` exposes additional entries.
Duration describes reported media length, not proof that the intended program ended.

`--private-schema-output` is optional and defaults off. It writes a **separate private
inventory** of custom-data paths, types and container entry counts; it retains no
scalar values. Names are untrusted data. Only short ASCII identifier-shaped names
are retained; other names and names containing token/auth/cookie/password/secret/
email/account/device/session are replaced with a fixed redaction marker, ignoring
case. Arrays use `[]` path components. These filters cannot establish that arbitrary
field names are non-private. Keep this inventory under ignored `runtime/`; never
copy it to stdout, commits, public reports or fixtures. Review any candidate names
privately before adding a specifically researched public parser. No public export
of the private schema is provided.

Both journals require a new path under `runtime/`, are opened exclusively with mode
0600, and are flushed/fsynced per record. Existing files and paths escaping through
symlinks are refused. Callbacks do no file I/O and retain at most 256 projected samples;
overflow drops the oldest sample and is explicitly counted by `dropped_samples`.
An end record reports deadline, interruption or backend failure without exception
text. Missing end records, truncation or dropped samples limit diagnostic completeness.
Neither a clean end record nor a `FINISHED` sample authorizes queue advancement.

This diagnostic is not a replay policy input. Preserve the existing lifecycle
capture and independent launch/control chronology alongside it when studying natural
endings. Any later policy change needs a separately reviewed provider contract and
tests; absent `breakStatus`, an empty object and elapsed duration are not sufficient
YouTube ad/completion evidence.
