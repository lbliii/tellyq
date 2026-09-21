# Synthetic Cast messages

`replay.json` is hand-authored synthetic protocol data. It is not a receiver
capture and provides no hardware evidence. App/session/content identifiers,
titles and the deliberately ignored pairing token are fictional. No private
runtime reports were used.

Each case gives an incoming message and the fields expected after normalization.
The ordered lifecycle covers media progress, buffering, ads, pause, an end marker,
empty status, malformed containers, error reporting and a replacement receiver
session. Tests also exercise invalid numbers that JSON cannot represent portably.
The explicit empty receiver case has a separate callback expectation: the pure
parser recognizes the shape, but the transport cannot publish app-absence proof
without a current matching GET_STATUS request.

`idle_receiver.json` is a separate synthetic regression fixture. The coordinator
reported that fresh GET_STATUS replies after a visually confirmed stop contained
isActiveInput, isStandBy, userEq and volume, with applications absent. This file
uses those field names and Open Screen's receiver implementation, with invented
values and request ID. It is not copied from a private log, and does not establish
that the changed transport has passed a hardware retest. Its expected idle
observation requires correlation to a pending GET_STATUS request; passive parsing
of the identical message must remain unknown/error.
