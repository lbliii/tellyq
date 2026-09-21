# Observed lifecycle regression excerpt

`a3-terminal-excerpt.jsonl` is a **sanitized live diagnostic excerpt**, not an
acceptance run or an original Cast frame. It derives from the private M2a A3
capture on 2026-09-21, using TellyQ `7a08929`, standard Python 3.14.0 and
PyChromecast 14.0.10. The original remains in ignored runtime state.

Selection is reproducible without publishing device or title identifiers:

1. Read the original `capture-a3.jsonl` in order through one `CaptureSanitizer`.
2. Retain records whose original `sequence` is `0, 70, 71, 72, 83, 120, 121, 122`.
   Record 0 is the begin record. Retain **every observation** in the seven selected
   windows, including anonymous media updates and receiver heartbeats.
3. Keep relative wall-clock and monotonic timing, ordering, gaps, unknown fields,
   state, positions and durations. Do not resequence, compress gaps, fill missing
   content/ad fields, or add an end record. No policy conclusions are copied.

The selected windows contain requested-content playback, an anonymous FINISHED,
different-content BUFFERING and PLAYING with the same media-session ID, and a
second anonymous FINISHED. The excerpt omits earlier launch/control activity and
intermediate windows. It cannot establish continuous observation, natural full-run
completion, visible playback, or the cause of the content change. A gap does not
assert that the receiver was silent; those events were deliberately excluded.
Neither terminal can be attributed to the requested content from media-session
identity alone. Unknown ad state remains unknown. Published duration and the
captured positions do not establish completion.

One ephemeral HMAC key produced consistent device, capture, application, app
session, content and numeric media-session pseudonyms across this excerpt. The
key and mapping are not retained or committed. Re-exporting produces different
pseudonyms; equality relationships and observations remain reproducible. The
original app-session/device/capture/content identifiers, device name, original
timestamps, titles, URLs, metadata, arbitrary errors and provider payloads are
absent. Only the public application name `YouTube` remains. A source-to-export
audit checked all original identity/title/name strings, numeric media IDs and
whole-window observation counts. `test_lifecycle_observed.py` further checks
the public allowlist and pseudonym shapes.

The fixture predates wire-shape diagnostics. It does **not** say whether the
original protocol omitted a field or normalization discarded it. No diagnostics
have been fabricated from null normalized values.

Tests labelled `synthetic_strengthening` copy this excerpt in memory, change all
record provenance to `synthetic`, supply inactive-ad flags, and optionally forge
old requested-content identity after the takeover. These changes test sticky
ownership loss; they are **not observed hardware evidence**. The committed excerpt
contains none of those mutations.
