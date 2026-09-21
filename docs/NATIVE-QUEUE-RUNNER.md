# Native YouTube foreground runner

Native mode lets YouTube continue to one approved successor within the existing
receiver session. It is explicit: copy [native-session.json](../examples/native-session.json)
into ignored `runtime/`, replace its device and queue IDs, and keep `"mode": "native"`.
Manifests without that field keep the existing release/idle/start behavior.
A queue's mode cannot be changed after creation. The example uses the quiet
[nature clips](LIVE-TEST-MEDIA.md) in an A → B → A sequence with unique item IDs.
Adjacent identical video IDs are declined because telemetry cannot reliably
distinguish their occurrences.

```sh
uv run --locked tellyq serve --manifest runtime/native-session.json
```

In another terminal, use the existing `start`, `status`, `pause`, `resume`, `stop`
and `shutdown` commands. Opening the service starts observation; an explicit
`start` is still required for the first item. Keep the service running to collect
completion evidence. Normal tests and this setup documentation do not play videos.

## What the owner does

After verified non-ad playback of A, the owner durably reserves B and records
dispatch before calling the optional backend's `play_next`. A command return does
not mark B current. The ordered observer preserves A's qualified ending and waits
for exact B identity plus fresh receiver identity and advancing non-ad playback.
Only then does the store create a native-observed B attempt. That attempt has no
fabricated START command or receipt. C may be staged only after B is adopted and
no reconciliation hold remains.

If B appears without A's observed ending, the approved B may be recognized while
A remains `needs_attention`. Further staging holds. Ads alone never prove B's
playback, and unrelated content is never adopted or stopped. Unknown enqueue
outcomes are not retried and never trigger the legacy release/start fallback.

Status exposes `queue.mode`, `reconciliation_required`, native reservations and
native operation outcomes separately from playback and completion. A reservation
is authorization history, not proof of remote playlist membership. At most one
reservation is outstanding locally; this does not bound YouTube's own playlist
or disable autoplay.

## Stop, shutdown and recovery

Stop first commits cancellation. When current ownership is fresh, it attempts
to clear an outstanding successor and separately exits the owned receiver app.
A rejected or unknown clear does not prevent a separately guarded STOP.
`session_exit_observed` records verified app exit; it does not certify an empty
remote playlist. If another session or unapproved content takes over, cleanup
holds instead of controlling that activity.

If STOP arrives during an expected A → B ad window before B has verified playback,
the current owner may retain the undispatched request for up to ten seconds. It
dispatches once only if fresh authorized B playback arrives in that window.
Status reports `stop_awaiting_native_observation` while waiting. A timeout, takeover
or connection reset drops this in-memory wait and leaves cleanup unconfirmed;
reopening never restores it. A journaled rejected or uncertain STOP is not retried.

Shutdown and Ctrl-C close the local owner without an implicit remote stop. One
already authorized successor may continue while disconnected, as approved by the
user. Reopening an existing native queue increments its owner generation and sets
a durable reconciliation hold, without inventing a user cancellation. It observes
fresh A or authorized B state without enqueueing, relaunching, clearing or retrying.
Persisted monotonic timestamps and old receipts never restore control authority.
Fresh matching observations can permit explicit controls, but status-only recovery
does not stage C. There is no automatic recovery override; deliberately resolve
the old session before creating a new queue.

The final item's natural completion is retained before explicit cleanup. The
checkpoint client can end collection when all native items are finished and then
perform its separately opted-in cleanup STOP. A stop or nominal runtime cannot
manufacture completion.

## Validation boundary

The implementation is exercised offline across the task, application, SQLite,
observation and service boundaries. Prototype native handoffs were observed on
hardware before this integration; they do not validate this composed runner.
The [M2 plan](M2-NATIVE-QUEUE-PLAN.md#acceptance-gates) still requires three
three-item hardware sessions, six verified handoffs, stop with a staged successor,
and bounded disconnect/reconnect checks. Preserve missing visual confirmation,
unproven clear semantics, remote autoplay and transport race limits in the results.
