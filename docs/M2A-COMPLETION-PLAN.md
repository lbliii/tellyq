# M2a: interpreting and attributing completion

This batch starts from merged `main` at `fe73b6d`. The metadata investigation
identified a usable provider field; it did not implement natural completion or
automatic queue advancement. The baseline passes 566 offline tests.

## Work streams

| Stream | Deliverable | Validation |
| --- | --- | --- |
| Completion core | Finite YouTube state interpretation at the adapter boundary; generic domain contracts for provenance, incremental identity and historical completion | Source-qualified state cases, conflicting/malformed input, ownership and freshness regressions, complete application flow |
| Checkpoint runner | One explicitly requested video per invocation, private journaling, isolated state, optional guarded pause/resume and cleanup | Fake-backend tests for command ordering, refusal, bounded collection and preservation of uncertain outcomes |
| Review and acceptance | Independent source/code review, exact combined checks, bounded live acceptance and checkpoint documentation | Both merge orders, Python 3.14 checks, reviewed live evidence with software and visual observations separated |

Each implementation branch targets `main` independently. Only the coordinator
controls the live receiver. The checkpoint runner uses the existing application
interfaces and can report unconfirmed outcomes against the baseline.

## Contract boundaries

The provider adapter is responsible for interpreting a reviewed finite set of
YouTube codes under a fresh, identified YouTube receiver. Ordinary states need
consistent standard Cast state. Ad-family codes, unknown codes, malformed input
and contradictory information cannot authorize completion. A missing Cast break
object is not sufficient evidence by itself.

The domain owns correlation and the completion decision. It may attribute a
qualified incremental terminal to recent ordered content history, while keeping
the terminal's original missing identity explicit. A reused media-session ID by
itself is insufficient. Replacement, reconnection, stale evidence, gaps, ads,
unknown media and explicit stop intent invalidate any chain that is not complete.

Historical completion and current receiver ownership are separate. An observed
ending must not disappear because the receiver later buffers or prepares content;
it also must not grant permission to control a replacement session. Completion is
consumed at most once by any future queue policy. This batch does not build that
queue policy or launch a next item.

The application must deliver short-lived terminal observations within its freshness
contract. Increasing freshness limits or using runtime as an ending timer is not
an acceptable fix for an oversized observation window.

## Acceptance sequence

1. Validate each branch and the exact combined tree offline, including packaging
   and isolated installation. Review source qualification and adversarial cases
   before operating the receiver.
2. Select the previously approved Projector by its saved private identity and
   require an observed idle receiver. Preserve the existing queue/session files.
3. Run three individually launched short programs across the two previously used
   NASA titles. Keep a persistent controller per run and preserve the launch,
   observation and command chronology. Include one guarded pause/resume cycle.
4. Record each actual terminal and a subsequent observation window. Evaluate
   natural completion separately from collection deadlines and cleanup stops.
   Ask for visual confirmation where it adds evidence; never fabricate it.
5. Clean up only a still-owned session, retain all refusals and failed attempts,
   and publish the precise capability and any untested ad behavior. No automatic
   advancement or new background service is enabled by these tests.

The [checkpoint ledger](M2A-CHECKPOINT.md#completion-candidate-validation) records
review, 666 combined offline tests and three supported live endings on candidate
`2599345`, including verified pause/resume and replacement cleanup refusal.
[PR #20](https://github.com/lbliii/tellyq/pull/20) and
[PR #21](https://github.com/lbliii/tellyq/pull/21) target `main` independently. The [provider research](YOUTUBE-METADATA.md)
pins the source evidence and distinguishes it from observations on this receiver.
