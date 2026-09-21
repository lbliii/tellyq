# M1 application integration

The existing one-program CLI now invokes `PlaybackApplication` through
`PlaybackBackend`, `SessionStore`, and `Clock`. The application and controller
import without PyChromecast, Zeroconf, Milo, or Chirp. Device imports occur inside
the Cast composition functions when a real connection or discovery is requested.
The default episode remains in `programs.py` and the validated version-1 queue.
There is no daemon, queue advancement, MCP server, new service, or UI.

## Boundaries

- `application.py`: start, status, and stop use cases; fresh ownership
  reconciliation; pure evidence-policy transitions; revision-checked storage.
- `cast_backend.py`: YouTube ID decoding, normalized Cast-to-domain translation,
  current connection generation, conservative app/media correlation, and bounded
  transport commands. `CastTransport` makes this adapter testable without sockets.
- `session_store.py`: private atomic JSON records with compare-and-swap revisions.
- `controller.py`: CLI composition, legacy queue compatibility, report encoding,
  and queue updates. `execute()` accepts a backend factory, store, clock, discovery
  function, and optional ownership resolver. A non-default store supplies its
  active snapshot through that resolver.
- `domain/policy.py`: deterministic evidence decisions. Receiver heartbeats update
  ordering but cannot refresh stale media evidence. Different media session IDs
  cannot form one progress pair. Reconnect, takeover, missing metadata, ads, and
  out-of-order callbacks cannot provide successful playback or natural endings.

A start command first observes a baseline, then dispatches, then establishes an
owned session from fresh exact-content observations. Old-app shutdown events
before that ownership point do not invalidate the new launch. An uncertain
command can acquire ownership only when a fresh session differs from baseline.
Status obtains new observations and never sends a playback command. Stop first
reconciles saved ownership against current app/session/content evidence. The
adapter checks ownership again before issuing the effect.

Socket reconnect signals invalidate cached identity and rotate the Cast
connection generation. An event-time watermark spans all kinds of messages, so a
late old receiver message cannot repopulate identity after an error or reconnect.
No media or app identity is restored from an earlier connection's cache.

## Evidence and stop behavior

Unknown ad state remains unknown. Actual Cast media messages currently provide
active/unknown ad evidence, not verified inactive-ad evidence. Consequently,
`observed_state: "playing"` and advancing raw positions can coexist with
`state: "unconfirmed"`, `evidence.reason: "ad_unknown"`, and
`receiver_playback_confirmed: false`. This is deliberately stricter than the old
controller, which treated missing ad data as false. Neither command acceptance
nor a historical visual report fills this gap. Fake fixtures that explicitly
supply `ad_active=False` exercise the generic positive playback contract; they do
not establish a real YouTube no-ad capability.

Stop means exiting the owned receiver application. A fresh explicit app-absence
observation after the stop boundary can establish `stopped` without inventing a
content `IDLE/CANCELED` event. A replacement app/session is a takeover, not proof
that our command stopped playback. The Cast transport's correlated idle-status
normalization is a separate independent change; this adapter consumes its
existing `kind: receiver, app_id: null` shape.

The queue becomes `unconfirmed` before a stop attempt and remains uncertain unless
stop evidence succeeds. Command receipts survive post-command observation or
storage failure. The controller writes returned receipts to the private run
report before waiting for post-command observations. A timeout remains uncertain,
and a repeated start is blocked until the user explicitly requeues or reconciles.

## JSON and recovery compatibility

All command reports now have additive `schema_version: 1`. Existing command
vocabulary and Cast observation fields remain available, including
`media_session_id`, `app_name`, and `kind: error` diagnostics. Added fields include
`observed_state`, evidence reason/natural-completion flags, command outcome,
`recorded_at`, attempt identity, and conservative capability reports.
`commands[].requested_at` is recorded before dispatch; `recorded_at` is the
receipt timestamp. Error results retain command outcomes. Unexpected transport
exception text stays out of public JSON and local diagnostics remain under
ignored `runtime/`.

Version-1 `queue.json` and unversioned/version-1 `session.json` stay readable and
are validated before device I/O. Legacy session files are not rewritten or
silently upgraded. A fresh reconciliation creates a version-1 snapshot under
`runtime/sessions/`, and `active.json` identifies the latest saved attempt.
Future versions, malformed records, missing referenced snapshots, and unknown
fields fail closed. Snapshot filenames hash opaque attempt IDs to prevent path
traversal. Writes and revision checks share a process lock and an in-process lock.

Durable snapshots contain request/content/target/session identity, revision,
historical state, and stop intent. They deliberately omit monotonic instants,
connection generation, latest observation, progress anchor, command receipt,
and playback evidence. Reloading creates a fresh generation with unknown current
state; stored history cannot confirm current playback, stop, or natural ending.
Same-process loads may return the immutable live snapshot while its durable
revision still matches. The active record is maintained separately from legacy
state, allowing recovery without changing the version-1 queue schema.

## Offline acceptance

The same backend contract tests run against an independent domain fake and Cast
with mocked transport. Application replays cover start transitions, partial,
stale, future, duplicate and reordered events, ads, takeover, reconnect, stop
boundaries, receipt preservation, and concurrent revision changes. A raw Cast
message replay goes through `normalize_message`, the Cast adapter, and the
application policy. JSON tests cover legacy recovery, compatible wire fields,
versioned failures, and no duplicate starts. Storage tests verify recovery without
monotonic evidence, atomic-write failure, private file modes, and revision checks.

All tests remain socket/DNS blocked. These are synthetic protocol and application
checks. They are not new hardware evidence or proof of full-episode completion.
The combined branch must pass CI after independent PR integration; a live
start/status/stop regression requires its own explicitly requested hardware task.
