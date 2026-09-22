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
| User visual observation | After the run, the user answered yes when asked whether they saw the forest clip play and then return to the Chromecast home screen. This is visual evidence distinct from receiver telemetry. |

The timed runner exited successfully and reported `start_acknowledged`,
`receiver_playback_confirmed`, `stop_acknowledged`, `stop_observed`, and
`owner_survived_mcp_exit` all true. Its generated summary retains
`visual_confirmation: null` and `live_acceptance: false` because it records
only what the timed runner knew. The later user reply and journal review are
preserved separately in private `review.json`. Together, the machine evidence
and visual confirmation satisfy the M3 hardware start/status/stop checkpoint.
Full M3 acceptance remains open because broader CLI/Python/MCP command
definition parity has not been completed.
