# M2b foreground-service checkpoint

Tested merged `main` at `b4ff7d303b584a8b9868268846adc79479cc8837` on
2026-09-21, after PRs #27–29. The merged tree passed 965 offline tests, Ruff,
formatting and ty. The identical integration tree also passed packaging and
isolated installation checks. This record separates those checks from hardware
observations and the user's visual confirmation.

**The foreground service works; M2b is not yet accepted.** Natural completion
settled durably, but the next item held. The active-stop client-visible timing
missed the ten-second target. Neither result establishes automatic handoffs.

## Natural completion, cleanup and reopen

An isolated two-item queue selected NASA's “Why Does NASA Study Earth?” followed
by “Dishing the Dirt.” The owner became ready without starting playback, a
competing owner was refused, and cached status remained responsive while the
owner worked. Explicit start was locally acknowledged in 104.02 ms; one cached
status query took 99.33 ms. Receiver identity and advancing playback were
verified. The user separately confirmed that NASA was visible and playing.

The first item ended naturally and persisted as `finished`. Release was neither
requested nor confirmed; the second item remained pending and was never started.
The public status did not expose the precise handoff hold reason or current
provider code. Do not attribute this particular hold to code 5 without evidence.
Earlier M2a traces contain FINISHED followed by same-content code-5 buffering and
a position reset; those remain separate observations with unresolved semantics.

STOP occurred after that natural ending, so this was cleanup, not an active
interruption. Local acknowledgement took 105.27 ms. A later cached receiver
observation gives an 8,780.86 ms upper bound from submission, not a precise
client-visible stop latency. Cancellation latched, the completed item stayed
finished, and the second item stayed pending. The user confirmed the Chromecast
home screen. After clean shutdown, reopening the same queue for status retained
canceled history, restored no playback authority, and issued no replay.

## Active stop

A second isolated two-item run verified the first item as owned and playing,
with receiver progression evidence and a current position of 24.107 seconds.
STOP was then submitted through a fresh CLI process.

| Measurement | Observed result |
| --- | ---: |
| Local STOP acknowledgement | 96.94 ms |
| First completed ticket plus client-read cached `stopped` | 10,431.17 ms |
| Receiver observation timestamp minus STOP receipt requested time | 4,467 ms |

The measured client-visible outcome missed the target by 431.17 ms. It includes
cold CLI startup, separate ticket and status subprocesses, and a 0.5-second polling
interval. The receiver timestamp uses a different boundary and cannot replace
that measurement. The checkpoint does not isolate the transport as the cause.

Exactly one acknowledged START and one acknowledged STOP were recorded. The
first item persisted as stopped, the second remained pending, and cancellation
latched. The user confirmed that the short run stopped and returned home.

All owners, including the reopened instance, exited normally and their endpoints
were absent at final inspection. Legacy queue/session source files were unchanged.
Private logs, device identity and state remain outside Git. No foreground
pause/resume or successful automatic handoff is claimed by these two runs.

## Remaining gates

The [closing workstreams](M2B-PLAN.md#closing-workstreams) target precise hold
diagnostics, prompt publication of verified stop evidence and reproducible
recovery/acceptance checks. Land and test those changes before the next supervised
hardware checkpoint. First prove one supported A → B transition. Then collect
three three-item sessions with six correct handoffs, foreground pause/resume and
canceling stop, and the documented crash-boundary checks. Unknown ads, content
replacement and unexplained provider states must retain their protective holds.
