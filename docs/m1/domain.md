# M1 domain contracts and evidence policy

This foundation adds a provider-independent core under `tellyq/domain/`. It does
not replace the existing controller, JSON records or CLI. The existing hardware
result remains the only live playback result; every new replay is synthetic.

## Public modules

- `values`: frozen, slotted dataclasses for exact content, target, request,
  capability provenance, command receipt/error, observation, display evidence and
  session snapshot. Shared values contain no mutable collections. Content titles
  and device names are presentation fields and do not affect identity equality.
- `ports`: structural `PlaybackBackend`, `SessionStore` and `Clock` protocols.
  Typed test doubles demonstrate the contracts without inheritance. The store
  requires atomic revision comparison and raises `RevisionConflict` without
  changing the saved snapshot on conflict. It defines no disk representation.
- `policy`: pure `observe`, `refresh`, `record_receipt`, `request_stop` and
  `record_display` transitions. Inputs remain unchanged; changed snapshots get a
  larger revision. A revision is a concurrency token, not a count of TV commands.

No module imports Cast, Milo, Chirp, the controller or persistence. None performs
I/O, reads a clock, generates an identifier or parses a provider URL. Runtime wire
codecs continue to belong in `tellyq/models.py` and their adapter boundaries.

## Ownership and time contract

The caller creates a `PlaybackScope` after reconciling the requested content with
an explicitly owned backend session. A scope combines a `PlaybackRequest`, opaque
backend session ID, connection generation and monotonic start boundary. A receipt
cannot establish ownership. An adapter may combine its app/media session IDs into
one opaque session identity, provided a replacement changes that identity.

The backend's `observe(target)` does not require a known session. The application
can read baseline telemetry, call `start(request)`, obtain fresh post-command
observations and reconcile their session/generation before creating a scope.
The bootstrap contract test demonstrates this flow entirely through the port.
Matching the first content ID is not sufficient ownership reconciliation when a
baseline or command outcome is ambiguous; that policy remains integration work.

The adapter increments observation sequence within one connection generation.
Monotonic observation times must come from the same process clock as the supplied
`now` and scope start boundary. Sequence and monotonic values are never compared
across generations. The caller creates a fresh scope after reconnect/restart and
must obtain fresh evidence; it cannot revive persisted monotonic proof. UTC/aware
record timestamps are descriptive and are not the ordering clock. `Clock` exposes
`utcnow() -> datetime` and `monotonic() -> float`; the policy receives plain values.

Observations are accepted only after the start boundary, from the exact target
and generation, with an increasing sequence and monotonic timestamp. Future,
stale, duplicate, reordered, foreign-target and old-generation observations are
discarded without extending the evidence lifetime. A fresh known different
session or content invalidates ownership permanently for this scope. Delayed
packets from the original session cannot reclaim it.

`refresh(snapshot, now=...)` is necessary when no callbacks arrive. By default
current evidence expires after five seconds. Expiration also discards the prior
progress witness for completion, so a telemetry gap cannot be bridged by one
late end message. These defaults are conservative synthetic-policy defaults,
not measurements of this Chromecast's callback rate; integration must select
an observation cadence and freshness window together.

## Evidence rules

1. Accepted/rejected/unknown command outcomes are independent of playback.
2. Exact content and session identity must both be present. Missing fields do
   not borrow previous values. Ad status has three values: active, inactive,
   unknown. Only explicitly inactive ads allow progress/completion proof.
3. Receiver playback requires PLAYING plus two increasing positions at least one
   second apart, with both samples still fresh. Frequent shorter samples retain
   the first anchor until a full interval is available. Pauses, buffering, unknown
   metadata, ads and backwards seeks break that pair. A reported PLAYING state
   alone may appear in the snapshot while its evidence remains unconfirmed.
4. Natural completion requires prior confirmed playback in the current fresh
   scope, exact identity, explicit IDLE/FINISHED and explicitly inactive ads. The
   observation adapter must use FINISHED only for a reliable content completion
   signal. Position reaching duration, elapsed wall time, app disappearance,
   provider autoplay and command acknowledgement do not prove completion.
5. Call `request_stop` before dispatching a remote stop effect. It immediately
   prevents natural advancement. Stop receipts retain that intent even if the
   remote outcome is unknown or rejected. Only an owned IDLE/CANCELED observation
   produces STOPPED; a receipt alone never does. Receiver-app exit verification
   remains an adapter/application integration task.
6. Display evidence records the target, content, source and timestamp separately.
   A historical user confirmation does not establish receiver progress or current
   TV power/input, and telemetry expiry does not rewrite the historical report.

`PlaybackEvidence.reason` explains why current proof is absent or present. Position,
duration and monotonic inputs reject bools, nonfinite numbers and values outside
finite floating-point range at construction/call boundaries. Positions/durations
also reject negative values. Record datetimes must include a timezone. This core
is not a general untrusted JSON decoder: adapters still validate and normalize
external enum/identifier/record shapes before constructing typed values.

## Verification and remaining integration

Offline tests cover ordered playback, insufficient progress, callbacks arriving
too quickly, seeks, pause/buffering, active and unknown ads, missing metadata,
stale/duplicate/reordered events, connection restart, content/session takeover,
command uncertainty, completion, stop intent, visual confirmation and revision
conflicts. A subprocess imports the complete core with `-I -S -B`, no site
packages, and an audit hook rejecting network and write side effects.

Wave 2 must adapt Cast into this backend contract, establish session ownership,
map normalized observations and explicit completion semantics, inject clocks and
stores, and route controller state through the policy. It must define disk/wire
versions and restart reconciliation, preserve CLI behavior, then exercise the real
adapter with the same offline contracts. No daemon, queue advancement, MCP API,
capability verification on hardware, or new free-threaded support is supplied by
this component alone.
