# Live test videos

As of 2026-09-21, use short nature clips without narration for future supervised
TV checks, per the user's preference. The selection is RSPB's “90 seconds in
nature” series:

| Slot | Video | YouTube ID |
| --- | --- | --- |
| A | [Abernethy Forest](https://www.youtube.com/watch?v=-6_mbMXxdfg) | `-6_mbMXxdfg` |
| B | [Insh Marshes](https://www.youtube.com/watch?v=5mI7HgjSi1Q) | `5mI7HgjSi1Q` |
| C, distinct-video diagnostic | [Inversnaid](https://www.youtube.com/watch?v=_ZwWo-fF3s0) | `_ZwWo-fF3s0` |

RSPB describes these as short visits to its reserves. The viewing descriptions
for [Abernethy](https://heypiko.co.uk/library/videos/rspb-abernethy-forest-90-seconds-in-nature)
and [Insh Marshes](https://heypiko.co.uk/library/videos/insh-marshes-90-seconds-in-nature-rspb)
identify ambient nature footage without a presenter or narration. A and B have
now played through the [native runner checkpoint](NATIVE-RUNNER-CHECKPOINT.md).
The user reported that they were extremely quiet. TellyQ changed neither mute nor
volume, and audio in that earlier run remains unconfirmed. C is another reserve
visit in the same series, with its title and RSPB authorship checked using YouTube's oEmbed
metadata for the [enqueue comparison](ENQUEUE-INVESTIGATION.md). That later
A → B → C run verified all three items, and the user confirmed visible advancement
and audible nature sound.

[The session example](../examples/session.json) uses A → B → A with distinct item
IDs, preserving the repeated-content checkpoint shape. Use a fresh queue ID and
runtime manifest for each new session; do not rewrite an existing durable queue.
Single-video checks accept any selected ID through `--content=ID`; the equals sign is
needed for A's leading hyphen. Use C when a distinct third video is needed to
compare with the repeated-content sequence; keep the chosen order in each private
manifest and checkpoint record.

The short duration keeps supervised runs manageable. Runtime, ads and buffering
still vary; only observed receiver evidence can establish completion. Changing
clips does not change the evidence policy or guarantee an ad-free run.

Earlier NASA and Bob Ross checkpoint records and replay fixtures retain their
original identities. They describe past runs and do not select future playback.
The legacy one-item Bob Ross demo remains separate from these live checkpoint
examples; use an explicit content ID or session manifest for the nature clips.
