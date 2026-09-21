# M2a pause and resume controls

`pause` and `resume` are explicit, bounded commands for an already owned program:

```sh
uv run --locked tellyq pause
uv run --locked tellyq status
uv run --locked tellyq resume
```

Both accept `--device` with the same exact target rule as `status` and `stop`.
They do not launch a title, seek, skip, or advance a queue. `stop` still exits the
YouTube receiver application. Normal tests do not contact a device; these commands
belong in an explicitly requested hardware session.

The application first observes fresh receiver and media identity. It refuses
unknown content, replacement apps/content, stale observations, a reset during the
observation window, an outstanding stop intent, and an absent optional controls
port. Pause requires observed PLAYING; resume requires observed PAUSED. A repeated
pause/resume in the wrong state is refused instead of restarting playback.

Cast reports its pause support through the integer `supportedMediaCommands`
bitmask. Missing, malformed or negative flags remain unknown. A current valid
pause bit is reported as **advertised**, with dated receiver provenance. It does
not establish a successful hardware test. Resume uses the standard Cast `PLAY`
message only for a fresh paused session that also advertises pause support; this
is a conservative route eligibility rule, not a separately advertised resume bit.
The protocol documents both the bitmask and the `PLAY` / `PAUSE` messages in
[Google's media playback message specification](https://developers.google.com/cast/docs/media/messages).

The implementation is pinned to PyChromecast 14.0.10. Its stock media play/pause
methods consult mutable current-session state. TellyQ instead uses its bounded
request/response path with an explicit media session, application session and
transport destination. The final send also checks current cached content and
state. A takeover after the check can still race a remote command; the envelope
continues to address the original session and never follows a replacement app.
There is no remote transaction or exactly-once promise.

## Additive report version 1 fields

- Observations add `pause_supported: bool | null`.
- Capabilities add `pause` and `resume`. `advertised` joins `unknown`, `verified`
  and `unsupported`; only fresh receiver-advertised support is established here.
- Control receipts use action `pause` or `resume`. `returned` / `outcome` describe
  the transport result independently from playback state.
- Control reports add `control_observed`. True requires an accepted command plus
  a fresh matching media state after dispatch in the same owned media session.
  It does not prove causation, advancing playback or a visible TV image.
- `observed_state` can be `paused` while `state` remains `unconfirmed`. Unknown ads
  remain unknown, so the strict playback/completion policy is unchanged.

An accepted command without its corresponding observation is not verified. An
explicit receiver rejection fails; a timeout, missing response or unfamiliar
response remains unknown. Exit code 0 means the command operation completed;
inspect `control_observed`, `observed_state` and playback evidence for its result.
A rejected or uncertain receipt returns exit code 1. Status never resumes media.

The JSON run report records unknown command intent before sending and then the
receipt. Snapshot CAS checks occur before the effect. JSON snapshots retain
ownership and stop intent across restart but deliberately discard old receipts
and monotonic evidence. They are not a durable transactional command journal;
M2's SQLite and runner work will own that boundary. Same-connection reconciliation
preserves prior strict playback history; reconnecting starts with fresh evidence.

Offline tests cover pause/resume, missing capabilities, takeover, unknown identity,
staleness, disconnect, stop intent, rejection, ambiguous results, persistence
failure and non-mutating status. The pinned library send-path test simulates an
immediate threaded reply and a mutable app-session change during sending. These
are synthetic contract checks. Live pause/resume and natural endings remain M2a
acceptance work.
