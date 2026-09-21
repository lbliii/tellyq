# One-program MVP

## User experience

The user asks the agent to queue and play one cozy program. A software command starts the requested episode on the living-room TV. The agent reports observed playback state and can stop the program.

The selected program is Bob Ross — Autumn Fantasy, season 20, episode 7, on the official YouTube channel. Video ID: `FozIp7Va7dY`.

## Existing setup

- Samsung 65-inch TV, approximately 2019; exact model is unknown.
- A Chromecast is attached to the TV. Its generation and whether it has a remote/app menu are not yet confirmed.
- The user confirms that a Google/Nest speaker can already start Netflix on this Chromecast.
- A Sonos home-theater system is attached through HDMI ARC. The user also has Google Assistant configured on Sonos.
- Original service priorities are Netflix, Disney+, Prime Video, and the Apple TV app, not an Apple TV hardware box.
- The user explicitly wants structured API/MCP control. Synthetic voice commands are not the chosen approach.
- A Raspberry Pi or custom HDMI device is a possible later fallback, not the first implementation.

## Implementation sequence

1. Discover Cast devices from the local Mac. Record friendly name, model, and stable device identifier. Resolve the living-room device explicitly; never choose the first discovered device by default.
2. Prove the transport with one direct YouTube video-ID command using PyChromecast's YouTubeController. The TV should initially be on the Chromecast input, and the Mac must be on a reachable home network.
3. Observe the result. Report app, content ID/title, player state, and position only where the receiver supplies them. Distinguish missing values from known values.
4. Once playback works, persist a one-item queue in a local JSON file.
5. Provide structured queue, start, status, and stop commands. A local command-line entry point returning JSON is sufficient initially. Reuse the same functions for a later MCP adapter.
6. Save a small run report containing requested content, target device, observed state, timings, and errors. Keep device addresses and session data out of Git.

## Minimal design

Agent -> local controller -> PyChromecast YouTubeController -> Chromecast -> TV.

One local process and one queue file are enough. The first build does not require Home Assistant, a hosted backend, new hardware, a graphical app, recommendation logic, multiple services, or recurring agent wakeups.

Suggested commands:

- `discover`: list device identity and model; does not initiate playback.
- `queue`: store the requested video and target device without playing.
- `start`: start the queued item and track the attempt.
- `status`: return fresh observations and their timestamp.
- `stop`: stop this playback session.

Queue state: `queued -> starting -> playing`. Preserve explicit `failed`, `unconfirmed`, `stopped`, and `finished` outcomes as appropriate. Mark `finished` only when supported by observed playback completion, not because the published runtime elapsed. Repeated status calls must not relaunch playback.

## Acceptance criteria

- The requested episode visibly starts on the intended TV.
- It continues playing, with advancing position observed where available.
- A software stop command works.
- A successful command response or an opened YouTube app alone does not count as confirmed playback.
- If receiver metadata is inadequate, obtain the user's visual confirmation and label that evidence separately. Do not claim machine-verified title identity.
- Ads, buffering, or unavailable status must not be treated as episode completion.

The first session can establish start/status/stop behavior without waiting through the entire episode. Automatic completion and advancing to a second program are the next milestone.

## Known risk and diagnostic boundary

PyChromecast has an open proposed YouTube compatibility fix (PR #1155). Test the released version first, inspect the actual failure if it occurs, and review a narrowly scoped fix if warranted. Do not assume the proposal is merged or verified on this hardware.

A public direct-media Cast test may help diagnose connectivity if YouTube fails, but it is a diagnostic only. It does not satisfy the real YouTube-program acceptance criteria or establish subscription streaming support.

## Current status

Planning and repository setup only. The user's latest request was to create a fresh local repository and open a Codex project. No implementation, dependency installation, pairing, network discovery, or playback has happened yet.
