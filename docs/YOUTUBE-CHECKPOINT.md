# One-run YouTube checkpoint

`scripts/checkpoint_youtube.py` runs one explicitly selected YouTube video on one
explicitly selected Cast receiver. It exercises the existing application, backend
and evidence policy, preserves the complete attempt chronology, and uses an
isolated session store. It never advances a queue, seeks, retries a command or
launches a second video. It is an operator tool for an authorized hardware test;
ordinary tests and CI use synthetic backends with network access blocked.

Run it from the tested checkout after `uv sync --locked` and the automated checks.
An existing private `runtime/queue.json` can supply the approved receiver UUID;
only its `device_id` is used for selection. A separate private JSON object with
that field also works. The UUID and receiver/session details are never printed.
The receiver must be idle: the tool requires a fresh explicit inactive receiver
observation before starting, then checks the application's second baseline again
at dispatch. An active or unknown receiver is refused.

```sh
uv run --locked python scripts/checkpoint_youtube.py start \
  --device-config runtime/queue.json \
  --content f9F7yDjSdNA \
  --output runtime/checkpoints/nasa-a-attempt-1 \
  --seconds 240 \
  --after-terminal 20 \
  --pause-hold 20 \
  --code-revision TESTED_GIT_COMMIT
```

Replace `TESTED_GIT_COMMIT` with the actual tested Git hash. This is explicitly
recorded as operator-supplied provenance; the tool does not assert that the working
tree is clean. Python, actual GIL status and installed PyChromecast/casttube
versions are recorded automatically. Omit `--pause-hold` for a natural-ending-only
attempt. Use a new output directory for every attempt, including refused or failed
attempts. Existing output is rejected before connecting to hardware.

`start` is mandatory. The exact content argument must be an 11-character YouTube
video ID. No receiver is selected from discovery results by position or friendly
name. `runtime/command.lock` prevents simultaneous cooperating TellyQ controllers;
run all hardware commands from the same working directory and designate one
hardware owner. That local lock does not exclude a phone, remote, another checkout
or another application from controlling the TV.

## Observation and control sequence

One persistent controller observes before launch and remains connected throughout
the attempt. Each observation batch retains domain observations plus the backend's
normalized wire records, with original timestamps and ordering. These wire records
are not full Cast protocol payloads. The tool does not access adapter internals or
turn a mutable backend into a read-only diagnostic backend.

The runner calls `PlaybackApplication.start`, then repeatedly calls `status`.
Accepted commands, policy evidence and visible TV outcomes remain separate. A
launch receipt without an owned session ends the attempt as unconfirmed; the tool
does not issue another launch to acquire ownership.

With `--pause-hold`, one pause attempt is eligible after fresh exact-content PLAYING
with owned app/session/generation and advertised or verified pause support. The
application takes another fresh baseline and retains its normal guards. A refusal
is journaled and is not retried. Only an observed owned PAUSED result permits the
hold and a resume attempt. The hold continues collecting status rather than
sleeping through it. Resume additionally retains the verified pause's media,
app/session and connection identities; a replacement, changed identity or exhausted
budget prevents dispatch. A resume receipt and observed PLAYING remain distinct
from later progress observations, which are retained in the journal.

Every observed FINISHED message is recorded as a **terminal candidate**, including
unknown/ad-related candidates. A candidate never shortens the attempt. Only the
application's `natural_completion_confirmed` evidence starts the subsequent
observation window. Its first snapshot is retained in a separate
`completion_evidence` record, and the summary preserves that historical result
even if the current state changes afterward. Cleanup stop cannot contribute to
this natural-completion aggregate.

The main observation budget is 15–600 seconds, default 240. The subsequent window
and optional pause hold are each 5–60 seconds. The main budget remains the upper
collection limit even if it cuts the subsequent window short; the stop reason then
stays `deadline`. A timer, reported duration, a position near the end and cleanup
stop never establish completion. Unknown completion can remain unknown for the
entire budget. Calls already in progress finish under the existing adapter/network
timeouts, and guarded cleanup runs afterward, so total wall time can exceed the
observation budget. The runner does not forcibly interrupt a remote command to
meet a stopwatch deadline.

## Cleanup and retained evidence

Cleanup only uses this invocation's isolated owned snapshot. A known replacement
or lost ownership is left untouched. Otherwise, `PlaybackApplication.stop` obtains
fresh ownership/content evidence and applies the existing backend guards before
dispatch. No ownership means no cleanup command. Refusal, accepted-but-unconfirmed
stop, cleanup error and observed stop remain distinct. An original error is
retained even if cleanup also fails. An interruption requests guarded cleanup;
killing the process cannot guarantee either cleanup or a final record.

All detailed output stays under ignored `runtime/`:

- `journal.jsonl` is an exclusive mode-0600 ordered journal. Its directory is
  mode 0700. Each record is flushed and fsynced; command intent is durable before
  the underlying effect. It includes environment, before/after legacy state hashes,
  request/options, phases, observations, intent, receipt, application results,
  terminal candidates, completion evidence, hold boundaries, errors and summary.
- `session-store/sessions/` holds only this attempt's revision-checked snapshots.
  The tool never writes the normal queue, legacy session or normal session store.
- `summary.json` is mode 0600 and contains software outcome flags, last state and
  reason **before cleanup**, separate cleanup state/outcome, failures and a check
  that legacy `runtime/queue.json` and `runtime/session.json` hashes are unchanged.
  `visual_confirmation` is always null; an operator records the user's observation
  separately with its actual time and scope.

Journal envelopes use schema version 1, a monotonically increasing record number,
UTC timestamp, process-local monotonic time and a `kind`. Nested application values
are serialized dataclasses, including new additive evidence fields when available.
The journal is private diagnostic evidence, not a public fixture or a stable
integration API. Exception types and typed guard diagnostics are retained without
arbitrary exception messages. Inspect and redact any excerpt before publication.

Stderr reports fixed phase labels; stdout reports a summary without device,
content, app/session identifiers or file paths. Exit 0 means collection and guarded
cleanup finished normally, with requested controls observed if enabled. It does
**not** mean natural completion passed: inspect `natural_completion_observed`.
Exit 1 indicates refusal, failure, requested-control failure, cleanup uncertainty
or changed legacy state; exit 130 indicates interruption. A connection failure
still writes an end record and summary when private output remains writable.

The [M2a checkpoint](M2A-CHECKPOINT.md) defines the larger acceptance set: three
supported natural endings across at least two titles, pause/resume evidence and
separate visible confirmation. Each invocation supplies one retained attempt.
This tool does not certify that set or relax the evidence policy.
