# M1 follow-up: observing the idle receiver after stop

The checkpoint on `main` at `a2b72a9` started the requested episode and the user
confirmed that stop returned the TV to the Chromecast home screen. TellyQ's
parser rejected subsequent receiver messages, so software verification stayed
unconfirmed. The coordinator reported that a separate fresh GET_STATUS probe
returned status objects with isActiveInput, isStandBy, userEq and volume, with
applications absent. This work uses that description; it does not read private
captures or establish a new hardware result.

## Evidence rule

The pure parser still recognizes an explicit `applications: []` as listing no
receiver app. The transport now requires current request correlation for any
app-absence observation, including that explicit empty list: a late reply to an
earlier poll must not prove that a new stop succeeded. Omitted applications now
has the same normalized meaning only when all of these conditions hold:

1. The receiver observer owns an outstanding GET_STATUS request on this
   connection. The reply echoes its exact positive integer request ID, excluding
   booleans and coercion.
2. The reply arrives before the earlier of the two-second polling deadline and
   the current observation window's end. The request has not been consumed,
   superseded, canceled, or invalidated by a socket transition.
3. The message is RECEIVER_STATUS with an object status, an absent applications
   key, an object userEq, and an object volume containing a finite level in
   `[0, 1]` and a boolean muted value. Present isActiveInput/isStandBy fields must
   also be booleans. These fields are shape checks, not evidence that a TV is on.

The normalized event remains `kind: receiver`, `app_id: null`,
`app_session_id: null`. Missing, malformed, expired, duplicate or unsolicited
evidence cannot manufacture that result. Explicit null or malformed applications
is not treated as omission. Existing app identity and media parsing are unchanged;
in particular, unknown ad state never becomes false.

## Protocol and transport grounding

Chromium's [Open Screen receiver implementation](https://chromium.googlesource.com/openscreen/+/refs/heads/main/cast/receiver/application_agent.cc)
populates applications only when an application is launched, always populates
userEq and volume, and echoes the request ID for GET_STATUS. Its
[protocol description](https://chromium.googlesource.com/openscreen/+/refs/heads/main/cast/protocol/streaming_session_protocol.md)
distinguishes solicited status replies from unsolicited notifications. This is
implementation evidence for omission meaning idle in a complete status reply;
it is not a guarantee that every Cast device produces the same fields.

The pinned [PyChromecast 14.0.10 receiver controller](https://github.com/home-assistant-libs/pychromecast/blob/14.0.10/pychromecast/controllers/receiver.py)
already interprets an absent applications key as no app. TellyQ takes a narrower
approach: its observer sends GET_STATUS through the same public send_message
method used by update_status and retains the outgoing dictionary. The pinned
[socket client](https://github.com/home-assistant-libs/pychromecast/blob/14.0.10/pychromecast/socket_client.py)
adds the request ID to that dictionary before writing bytes. Normal observation
handling matches the echoed ID, without a second callback event or retained
library response callbacks. A mocked-socket test exercises these actual send
methods to make this dependency assumption explicit.

The observer publishes its pending request before sending and holds no lock
across transport I/O, so a reply can arrive before send_message returns. Matching,
timestamping, queue transfer and retirement share one lock. Observation windows
retire pending correlation in a finally block; socket status notifications also
retire it because PyChromecast resets request IDs on reconnect. Late baseline
replies therefore cannot become fresh post-stop app-absence evidence. Channel
teardown and connection-context cleanup also retire requests. Nonempty app lists
remain passive identity observations; their broader session and freshness policy
belongs to application integration.

Socket LOST, DISCONNECTED and CONNECTING statuses also publish a timestamped
`kind: error`, `type: CONNECTION_RESET` observation. The symbolic status is its
reason; addresses and service data are omitted. CONNECTED emits no reset event.
Initial CONNECTING may emit one before bootstrap. The application adapter uses
this boundary to invalidate cached identity and rotate its connection generation.

## Validation and remaining work

`tests/fixtures/cast/idle_receiver.json` is hand-authored synthetic data based on
reported field names and the public implementation, with invented values. Tests
cover accepted idle replies, normal applications, explicit empty applications,
invalid/missing fields, request mismatch, expiry, duplication, supersession,
disconnect, send failure, observation failure and response-before-send-return
concurrency. Tests block sockets and DNS. No discovery or playback is performed.

The new rule deliberately leaves other incomplete/unknown idle shapes
unconfirmed. A dropped or slow reply can be retried by the next status poll;
deadline expiration is never itself proof of app exit. App absence proves a
receiver-app observation, not TV power, HDMI input, video visibility or natural
content completion. Application integration owns stop-state persistence and
post-command/session policy. A fresh explicitly requested live start/status/stop
check remains necessary after integration; this synthetic regression is not a
substitute for that check.

Independent branch validation on standard CPython 3.14.0: `uv sync --locked`
and `uv run --locked poe ci` passed with **301 tests**, **93.0%** branch-inclusive
coverage, Ruff, ty, source/wheel builds and isolated installation/CLI smoke.
Dependencies and lock are unchanged. These results precede combined integration
and do not mark M1 complete.
