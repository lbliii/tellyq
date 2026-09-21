# Sanitized native YouTube queue observations

These selected observation windows come from the supervised native queue prototype
on 2026-09-21, using the existing controller and domain evidence rules at
`39c4ed0`, PyChromecast 14.0.10 and locked casttube 0.2.1. They are historical
observations, not full-run captures, raw protocol frames, foreground-service
integration, durable queue ownership, or exactly-once dispatch evidence. The
[checkpoint](../../../docs/NATIVE-QUEUE-CHECKPOINT.md) records command chronology,
visual evidence and experiment limitations separately.

| Fixture | Source experiment | Retained source record sequences | Observations |
| --- | --- | --- | --- |
| `play-next-04.jsonl` | Clean `play_next` repetition, no queue readback | 0, 119–123, 125–136, 138 | 53 |
| `play-next-02.jsonl` | Earlier `play_next`, failed queue readback before transition | 0, 122–126, 128–139, 141 | 54 |

Each excerpt contains four complete pre-terminal windows, the terminal window,
and every following observation window through verified B progress plus one
additional window. Missing source sequence numbers inside those ranges are
derived notification records, not omitted observation windows. Every observation
inside a selected window remains in source order, at its recorded relative time;
window-return times also remain intact. Run 02 includes A's terminal and the
first B identity in the same window. Run 04 splits them across adjacent windows.

The private journal's adapter observations pass through `CaptureSanitizer` with
the original capture clock baseline. A final consistent alias mapping replaces
opaque pseudonyms with clearly synthetic device, capture and session labels and
the synthetic numeric media ID `7001`. The public requested YouTube IDs remain
`f9F7yDjSdNA` (A) and `hgsIFyITvJE` (B); the public YouTube app ID remains
`233637DE`. Both contents used the same recorded media-session identity. The
aliases describe equality within a fixture only and do not assert a shared
session between runs. No original connection identifier is exported; the replay
adapter creates a fresh local connection generation.

Wall clocks shift to 2000-01-01, and monotonic clocks are relative to each original
begin record. Positions, duration changes, ordering, omitted identities, wire
field shapes, unknown Cast ad flags and finite provider codes are preserved.
Names, titles, addresses, paths, credentials, pairing values, arbitrary provider
metadata and original session identifiers are absent. The private logs and alias
mappings are not committed. No saved policy conclusions or visual confirmations
are imported; no end record is fabricated. `entire_run_observed` stays false and
`external_control_unobserved` stays true.

Tests replay the observations through the existing read-only `CastBackend` and
`LifecycleTracker`. One declared request tracks A; a separate declared request
tracks expected B. Neither scope issues commands. A's ordered-history completion
survives unknown states, replacement and ads. A's ownership is still lost when
the content changes to B. The B scope confirms playback only after fresh exact
identity and advancing non-ad positions; eight (run 04) or nine (run 02) ad
samples cannot do so. The replay does not authorize that scope in the production
runner, adopt B into A's scope, infer an enqueue acknowledgement, reconstruct a
remote queue, or establish recovery safety.
