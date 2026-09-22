# First supervised M3 MCP checkpoint

Run on 2026-09-22 against merged `main` at `99fa024` with Python 3.14 and
Milo 0.4.3. The target was the same exact Chromecast UUID used in prior
playback checkpoints, freshly discovered before the run. Its UUID, private
manifest, owner state and unabridged journal remain under ignored
`runtime/checkpoints/mcp-20260922T151104Z-c97e6b/`. The manifest selected one
YouTube item: **Abernethy Forest | 90 seconds in nature** (`-6_mbMXxdfg`).

The foreground owner reached `ready` without launching content. A real Milo
stdio client completed a status-only trace and matched the owner queue and
target to the private manifest. The opt-in live client then sent `start`, exited,
reopened MCP for status polling, sent a guarded `stop`, and closed. The owner
remained alive across the MCP process exits.

| Evidence | Observation |
| --- | --- |
| Start command | MCP returned `accepted` at 15:12:01 UTC. This was only a command receipt. |
| MCP start window | The 20-second window expired with no playback view in its status samples. The runner correctly reported `receiver_playback_confirmed: false`. |
| Stop command | MCP returned `accepted` at 15:12:22 UTC. The 15-second cleanup window also expired before a stopped view appeared, so the runner reported `stop_observed: false`. |
| Durable start ticket after the window | `handled` without failure; the matching forest clip had `state: playing`, `receiver_playback_confirmed: true`, inactive ad evidence, and position 21.591 seconds at 15:12:43 UTC. This is receiver evidence, not a visual TV observation. |
| Durable stop ticket after the window | `handled` without failure; the same attempt had `state: stopped`, `ownership_lost: false`, and `reason: stop_observed` at 15:12:45 UTC. |
| Later real MCP status | At 15:13:29 UTC, a fresh stdio trace showed `stopped`, the stop receipt and `stop_observed`. |
| User observation | The user did not watch the clip. They later saw the TV on at the Chromecast home screen. This supports a post-run home-screen observation, not visual confirmation of playback or the stop transition. |
| Shutdown | The owner returned a stopped snapshot and closed cleanly after the TV stop evidence. |

The runner's summary remains a **failed timed checkpoint**: its two observation
flags are false and `live_acceptance` is false. The later tickets and MCP status
establish receiver-confirmed playback and observed stop, but do not retroactively
change what the timed client observed. This is a qualified real-hardware result,
not full M3 acceptance. In particular, visual playback was not confirmed by the
user, and the MCP client did not observe playing within its configured window.

The start operation stayed active beyond the original 20-second window; the
guarded stop waited behind it. The checkpoint runner's default windows are now
75 seconds for start evidence and 30 seconds for stop evidence, still bounded and
ending early when proof arrives. A future supervised run should use a fresh
queue ID and output directory. It must record command receipts, receiver
evidence and the user's visual observation separately. No automatic retry is
justified by an accepted ticket alone.
