# M1 stream C: defensive Cast observations

`tellyq.cast_messages` normalizes unknown JSON messages without importing
PyChromecast or touching clocks, sockets or files. `tellyq.cast.media_observation`
remains available as a re-export. Discovery, connection and command behavior are
unchanged. The passive observer timestamps and queues the normalized record.

## Boundary behavior

- Only allowlisted scalar fields leave the parser. Invalid containers, strings,
  booleans and numeric values stay unknown (`None`), without coercion. Position
  and duration reject bools, negative values, infinity, NaN and overflowing
  integers. Session IDs must be nonnegative integers, excluding booleans.
- Explicit empty media status retains the existing `{kind: "media",
  empty_status: true}` JSON shape. Missing or malformed status reports
  `empty_status: null`; a valid status object reports false. Neither an empty
  media message nor an empty receiver message reuses previous identity/state.
- The first status/app remains the selected entry, preserving current behavior.
  Invalid first entries do not silently select a different session. Extended
  media is considered when primary media is absent/null; malformed primary
  metadata is not replaced with a different source to fabricate valid data.
- Each call returns a new dictionary containing immutable scalar values only.
  The callback adds timestamps in another owned dictionary before enqueueing.
  A transport callback can mutate its original nested payload after returning
  without changing the consumer's evidence; consumers own dequeued records.
- Unknown message types are ignored. Recognized malformed messages produce
  observations with unknown fields. Malformed receiver status instead produces
  an `INVALID_RECEIVER_STATUS` error: the legacy controller interprets
  `kind: receiver` with `app_id: null` as an app exit, so this shape is reserved
  for a valid explicit `applications: []`. A nonempty application must have
  nonempty string app/session IDs. Optional display/input/standby fields may
  remain unknown. This prevents malformed data from falsely confirming stop.
  Error reasons retain bounded uppercase
  symbolic codes, not free-form diagnostics or credential URLs; custom data,
  screen IDs and pairing tokens are never copied into observations.

## Ad evidence and policy limits

A valid current break ID, clip ID or elapsed break time reports `ad_break: true`,
following Google's [BreakStatus fields](https://developers.google.com/cast/docs/reference/web_sender/chrome.cast.media.BreakStatus).
Absent, null, empty or malformed break status remains unknown. This parser has no
verified explicit no-ad signal, so it does not emit `ad_break: false`. The wire
type allows `bool | None` for a future adapter with such a signal.

Google's [media message contract](https://developers.google.com/cast/docs/media/messages)
allows partial updates; omitted content metadata stays unknown in the current
observation. Correlation belongs to policy, not parser carry-forward. The parser
preserves observed player/end markers without declaring successful playback or
natural completion.

The legacy controller/evidence code still treats unknown ad state as falsy.
Changing that policy, correlating observations to connection generations and
app/session ownership, and adopting the new domain contracts are wave-2 work.
This PR therefore improves recorded evidence but does not claim to fix all
false completion or takeover risks by itself.

## Verification

The hand-authored fixtures in `tests/fixtures/cast/` are labeled synthetic and
contain no device captures or local runtime data. Offline replay checks exact
normalized records and callback output for normal/partial status, buffering,
pause, ads, end markers, empty state, malformed data, errors and takeover.
Additional tests cover invalid numeric values, malformed nested shapes, producer
thread ownership and an isolated import with all Cast dependencies unavailable.
Existing baseline tests remain unchanged. No hardware discovery or playback is
part of this work; replay results are not new hardware evidence.

Validation on standard CPython 3.14.0: `poe check` and `poe preflight` passed with
79 tests, Ruff, ty, wheel/sdist build and isolated install checks. The source
archive includes both synthetic fixture files, verified separately. Dependencies
and the lock are unchanged; preflight ran offline using the existing uv cache.
