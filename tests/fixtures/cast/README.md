# Synthetic Cast messages

`replay.json` is hand-authored synthetic protocol data. It is not a receiver
capture and provides no hardware evidence. App/session/content identifiers,
titles and the deliberately ignored pairing token are fictional. No private
runtime reports were used.

Each case gives an incoming message and the fields expected after normalization.
The ordered lifecycle covers media progress, buffering, ads, pause, an end marker,
empty status, malformed containers, error reporting and a replacement receiver
session. Tests also exercise invalid numbers that JSON cannot represent portably.
