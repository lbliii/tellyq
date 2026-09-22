# Second supervised M3 MCP checkpoint

Run on 2026-09-22 against merged `main` at `e96e67e` with Python 3.14 and
Milo 0.4.3. The exact previously tested Chromecast was freshly discovered.
The private one-item manifest selected **Abernethy Forest | 90 seconds in
nature** (`-6_mbMXxdfg`) with a new queue ID. The device identity, manifest,
owner state and unabridged journal remain under ignored
`runtime/checkpoints/mcp-20260922T153300Z-cf6dfc/`.

The owner reached `ready` without playback. A real stdio MCP client completed
a status-only trace and matched the complete owner queue and target to the
private manifest. The live client then sent `start`, closed, reopened for fresh
status, sent a guarded `stop`, and observed stopped playback. The owner survived
the MCP exits and later shut down cleanly.

| Evidence | Observation |
| --- | --- |
| Start command | MCP returned `accepted` at 15:33:53 UTC. This was a command receipt, not playback proof. |
| Receiver playback | MCP status at 15:34:33 UTC showed the exact content ID, `playing`, position 23.568 seconds, `ad_active: false`, matching start receipt, and `receiver_playback_confirmed: true` with reason `progress_confirmed`. |
| Stop command | MCP returned `accepted` at 15:34:33 UTC. This was a command receipt, not stop proof. |
| Receiver stop | MCP status at 15:34:35 UTC showed the same attempt `stopped`, `ownership_lost: false`, the matching stop receipt, and reason `stop_observed`. |
| Owner lifetime | The runner reported `owner_survived_mcp_exit: true`; later owner shutdown returned a stopped snapshot and closed its process. |
| User visual observation | Pending the user's response after the run. |

The timed runner exited successfully and reported `start_acknowledged`,
`receiver_playback_confirmed`, `stop_acknowledged`, `stop_observed`, and
`owner_survived_mcp_exit` all true. Its generated `visual_confirmation` remains
null and `live_acceptance` remains false until the user's separate observation
is recorded and the journal is reviewed. This run satisfies the machine-side
MCP start/status/stop checkpoint. Do not claim visible playback or full M3
acceptance from the machine summary alone. Broader CLI/Python/MCP command
definition parity remains open regardless of the visual result.
