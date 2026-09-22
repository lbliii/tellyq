# M3 acceptance candidate

The local Milo 0.4.3 MCP surface exposes exactly `start`, `status`, and `stop`
for an already-running foreground owner. The same command metadata selects
Milo registration and CLI arguments. CLI, direct Python dispatch, Milo shell,
and MCP return the versioned `MCPResult` envelope, including structured errors.
The owner keeps monitoring when the MCP client exits.

The advertised MCP input schemas match the intended command arguments. Its
output schema names the stable envelope fields, marks `response` and `error`
optional, and describes the error object. The nested owner response is opaque
in the advertised schema because Milo 0.4.3 misrepresents its nullable fields
and recursive aliases. The returned JSON and Python `MCPResult` retain the full
owner response. Clients must inspect timestamped status evidence; an accepted
start ticket alone is not playback proof.

Offline acceptance on this candidate: `uv run --locked poe check` passed Ruff,
formatting, ty, and 1,384 tests. The suite includes real stdio initialization,
tool listing and calls, malformed inputs, success and error envelopes, stdout
cleanliness, MCP exit while the owner remains available, and installed-wheel
Milo verification. These tests use only fake local Unix sockets.

The [supervised MCP checkpoint](M3-MCP-SECOND-CHECKPOINT.md) separately passed
the hardware gate: exact forest clip receiver playback and progress, accepted
stop with observed stopped state, owner survival after MCP exit, plus the user's
visible playback and home-screen confirmation. No new TV run was needed for
this schema change.

M3's three-tool owner surface meets the [roadmap acceptance](ROADMAP.md).
The known nested-schema limitation and the intermittent native enqueue stall
([issue #42](https://github.com/lbliii/tellyq/issues/42)) remain recorded.
M4 begins with a bounded device and service capability investigation.
