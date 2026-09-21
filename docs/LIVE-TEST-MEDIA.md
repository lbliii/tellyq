# Live test videos

As of 2026-09-21, use short nature clips without narration for future supervised
TV checks, per the user's preference. The selection is RSPB's “90 seconds in
nature” series:

| Slot | Video | YouTube ID |
| --- | --- | --- |
| A | [Abernethy Forest](https://www.youtube.com/watch?v=-6_mbMXxdfg) | `-6_mbMXxdfg` |
| B | [Insh Marshes](https://www.youtube.com/watch?v=5mI7HgjSi1Q) | `5mI7HgjSi1Q` |

RSPB describes these as short visits to its reserves. The viewing descriptions
for [Abernethy](https://heypiko.co.uk/library/videos/rspb-abernethy-forest-90-seconds-in-nature)
and [Insh Marshes](https://heypiko.co.uk/library/videos/insh-marshes-90-seconds-in-nature-rspb)
identify ambient nature footage without a presenter or narration. This selection
has not yet been verified on the Chromecast.

[The session example](../examples/session.json) uses A → B → A with distinct item
IDs, preserving the repeated-content checkpoint shape. Use a fresh queue ID and
runtime manifest for each new session; do not rewrite an existing durable queue.
Single-video checks accept either ID through `--content=ID`; the equals sign is
needed for A's leading hyphen. Future private probes should use the same pair.

The short duration keeps supervised runs manageable. Runtime, ads and buffering
still vary; only observed receiver evidence can establish completion. Changing
clips does not change the evidence policy or guarantee an ad-free run.

Earlier NASA and Bob Ross checkpoint records and replay fixtures retain their
original identities. They describe past runs and do not select future playback.
The legacy one-item Bob Ross demo remains separate from these live checkpoint
examples; use an explicit content ID or session manifest for the nature clips.
