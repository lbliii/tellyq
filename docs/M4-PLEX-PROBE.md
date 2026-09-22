# M4 Plex feasibility checkpoint

This checkpoint selects Plex as TellyQ's next provider candidate and records the
environment gate discovered before adapter work. It is not a Plex adapter or an
M5 playback acceptance run.

## Why Plex is the candidate

Plex exposes the semantic controls that were missing from the Netflix probe.
Home Assistant's [Plex integration](https://www.home-assistant.io/integrations/plex)
documents library searches for exact movies and episodes, client playback
commands, current-item metadata, progress, and playback controls. Its
[Google Cast integration](https://www.home-assistant.io/integrations/cast#plex)
also documents sending media from a configured Plex server directly to a Cast
target. TellyQ can investigate these local APIs directly; installing Home
Assistant is not a prerequisite.

The supported catalog is the media available through a configured Plex Media
Server. This route does not provide programmatic access to Netflix, Disney+, or
other subscription catalogs. Plex's own free streaming catalog is also outside
this checkpoint until it exposes the same exact-item and playback evidence.

## Environment probe

On 2026-09-22, an eight-second local `_plexmediasvr._tcp` discovery found no Plex
Media Server. The private discovery result remains under ignored
`runtime/plex-probe/`; no addresses or account data are committed.

The paired Android TV Remote endpoint rejected both `plex://` and the
`com.plexapp.android` package launch route. The active-app report stayed on the
Google TV ambient application. This did not prove that a native Plex client can
be controlled on this device. A native Android TV client is optional for the
first experiment because the existing Google TV is also a proven Cast target.

The official Plex download manifest supplied Plex Media Server
`1.43.4.10903-e5521bd8c` for macOS. Its archive matched the manifest's checksum,
but macOS rejected the unpacked application and local code-signature verification
failed. TellyQ did not bypass that operating-system check or run the rejected
binary. The archive and all helper scripts remain ignored runtime material.
Homebrew could not provide an alternate trusted installation because this Mac's
Xcode license is currently unaccepted.

## Setup gate

The next live checkpoint requires:

1. Install Plex Media Server through a macOS path accepted by Gatekeeper, then
   complete Plex's browser-based sign-in and server setup.
2. Add a small test library containing two user-owned or openly licensed videos.
3. Keep the server and Google TV on the same local network. Local playback avoids
   Plex's separate remote-playback requirements.
4. Save any Plex token, server address, machine identifier, and client identifier
   only under `runtime/plex-probe/`.

Plex's [setup guide](https://support.plex.tv/articles/200264746-quick-start-step-by-step-guides/)
documents the server, account, library, and player setup. Its
[Companion matrix](https://support.plex.tv/articles/203082707-supported-plex-companion-apps/)
classifies Android TV and Chromecast as receiver devices.

## Bounded acceptance run

Once the setup gate is satisfied, the first experiment will use the existing Cast
target and the Plex server directly:

1. Discover the server and query the test library without logging secrets.
2. Resolve one exact title to a stable Plex key.
3. Start that item on the intended Cast target.
4. Verify title identity, playing state, and advancing position from independent
   observations.
5. Verify pause, resume, and stop.
6. Observe one natural ending without inferring it from published duration.
7. Repeat with a second title, then exercise YouTube-to-Plex and Plex-to-YouTube
   ownership handoffs.

Only after steps 1-5 pass should an adapter be added to TellyQ. Natural completion
is required before Plex can participate in unattended queue advancement.

**Current classification:** promising and setup-blocked. Plex is the selected M4
candidate, but no exact-title launch or playback telemetry has yet been observed
on this equipment.
