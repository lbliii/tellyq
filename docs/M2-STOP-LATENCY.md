# M2 stop responsiveness

This change starts from merged `main` at `b4ff7d3`. It changes how the existing
observation deadlines are used; it does not change the playback evidence policy,
receiver polling interval, remote quit timeout, or ownership requirement.

## Measured trigger and separate clocks

The coordinator's active STOP checkpoint accepted the local request in **96.94 ms**.
The first fresh client read containing both a completed ticket and STOPPED took
**10,431.17 ms**, exceeding the healthy-LAN ten-second target. That client interval
starts at fresh STOP CLI launch and includes fresh ticket/status CLI launches and
0.5-second polling. It is not a transport-only latency measurement.

Receiver stop evidence was observed about **4,467 ms after the STOP receipt's
`requested_at` timestamp**.
That interval has a different origin; subtracting it from the CLI interval would
not isolate publication delay. The old application always waited for a two-second
ownership baseline and then the full six-second post-stop observation window,
even when sufficient correlated evidence had already arrived. Normal owner
observation also contributes queue wait before stop handling.

Keep these phases separate in the live acceptance record:

1. Client invocation through local ticket acceptance, then owner queue wait.
2. Fresh ownership baseline, durable stop preparation and remote command return.
3. Receiver evidence arrival and the application evidence decision.
4. Durable snapshot/queue settlement, ticket publication and first fresh client read.

Compare durations only within their recorded clock and origin. No new timestamps
or wire fields are added by this change.

## Prompt observation with the same safety boundaries

`PlaybackObservationUntil` is an optional domain port. Its readiness callback sees
the cumulative, ordered observations consumed by the call. A backend that lacks
this capability keeps its existing `observe` behavior. The foreground task's
guarded backend forwards the capability while retaining its observation and
release bookkeeping.

Cast's optional transport capability delivers each newly drained callback batch
once. The adapter normalizes the whole batch, updates identity history, and only
then asks the application whether the cumulative evidence is sufficient. The
returned transport list contains exactly the events delivered to that callback.
Raw retention still covers the whole window, even when persistent observation
retains only the latest window. Interrupted reads preserve consumed/pending raw
diagnostics and retire receiver request correlation.

Only operator STOP selects this path. Its baseline can return early after fresh
receiver ownership and fresh exact-content media identity pass reconciliation.
A receiver heartbeat alone does not substitute for fresh media: it retains the
normal two-second baseline so stale cached media cannot hide a replacement.
At the ordinary deadline the existing reconciliation rules still apply.

After dispatch, the application uses its existing pure stop-evidence policy.
A STOPPED candidate must retain ownership; accepted quit alone is insufficient.
Connection observation wakes on callback arrival instead of sleeping until the
next two-second poll. It drains queued callbacks before deciding, checks again
for callbacks queued during evaluation, retires the request under the observer
lock, and drains again. A candidate revoked during retirement resumes polling
within the original deadline. The final returned events determine the result,
even if a last callback revokes an earlier candidate.

The post-stop budget remains six seconds. Missing, stale, malformed or
uncorrelated receiver responses never establish stop. Late baseline idle replies
cannot borrow a post-command request's correlation. Replacement, ad and reset
events in the same drained batch cannot be hidden by an earlier idle candidate.
The owner persists the resulting snapshot and queue outcome before publishing
its completed ticket as before.

## Offline verification and remaining acceptance

Independent locked Python 3.14 validation passes `uv sync --locked --offline` and
`uv run --locked poe check`: **980 tests**, Ruff, formatting and ty. The AF_UNIX
subprocess checks need the sandbox's local socket restriction lifted; their
Internet-socket and DNS guards remain enabled. No dependency or lockfile changed.

The focused tests drive the real receiver observer, connection loop, Cast
normalization and application using a virtual clock and queue. A synthetic fresh
baseline at 40 ms, quit return after another 100 ms, and correlated idle after
another 20 ms complete in 160 ms of virtual time. A silent accepted quit instead
uses the full six-second verification budget. These values demonstrate removal
of fixed waits; they are not measurements of a receiver or a network.

Regressions cover receiver-only baselines, fresh and queued content replacements,
idle followed by queued replacement/ad/reset, late baseline replies, contradictions
queued during readiness or request retirement, partial diagnostics after failure,
whole-window raw retention, and durable foreground STOP through the guarded
backend. Existing fake backends continue exercising the fixed-window fallback.
The runner's existing blocked-observation tests separately cover responsive
cached status, a queued priority stop, immediate cancellation, and owner-only
dispatch after the bounded observation returns.

This does not interrupt an in-flight remote command or ordinary observation.
The serial owner can still wait for a current operation, including a longer start
verification, and quit still has its ten-second transport timeout. There is no
unconditional ten-second guarantee under faults or during another long operation.

**Live stop-latency acceptance remains open.** The coordinator must repeat the
same active foreground STOP measurement from fresh CLI invocation to fresh
completed-ticket plus STOPPED read on the reviewed combined code. Record local
acceptance, receiver observation, publication/client visibility and the user's
visible result separately, retaining the failed baseline measurement. No hardware
discovery, playback or live stop was performed for this branch.
