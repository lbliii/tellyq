# Research handoff

Checked 2026-09-21. The references below describe researched capabilities; actual
hardware results are recorded separately in `IMPLEMENTATION.md` and ignored
`runtime/runs/`.

## First prototype

- [PyChromecast](https://github.com/home-assistant-libs/pychromecast): open-source Python library for Cast discovery, app channels, media control, and status. Current README requires Python 3.11+. Network discovery uses mDNS; the local host needs a reachable network path to the Cast device.
- [YouTube controller](https://github.com/home-assistant-libs/pychromecast/blob/master/pychromecast/controllers/youtube.py): exposes `play_video`, `add_to_queue`, and `play_next`; internally uses casttube. Do not treat a YouTube watch URL as a directly playable media file URL.
- [YouTube example](https://github.com/home-assistant-libs/pychromecast/blob/master/examples/youtube_example.py): shows discovery by device name, handler registration, and playback by video ID.
- [Open compatibility proposal, PR #1155](https://github.com/home-assistant-libs/pychromecast/pull/1155): reports a YouTube lounge API HTTP 400 problem and proposes setting Content-Length. Author reports a Nest Hub test; this is not validation on the user's Chromecast.
  Rechecked during implementation: still open. Released PyChromecast 14.0.10 worked
  on this Chromecast without the proposal; retain it only as a diagnostic lead if
  a future run actually exhibits the error.
- [Bob Ross — Autumn Fantasy](https://www.youtube.com/watch?v=FozIp7Va7dY): official-channel full episode, season 20, episode 7. Exact-ID launch, visible playback, advancing position and stop were verified on the user's Chromecast on 2026-09-21; full-episode completion is still untested.

### Native YouTube queue checkpoint

On 2026-09-21, two isolated experiments using the installed controller's
`play_next` verified a natural ending followed by the exact requested successor
and advancing playback, with positive YouTube ad-state observations between
programs. Neither exited/relaunched the app between items. The second success
used no queue readback. A separate `clear_playlist` plus app-exit experiment
verified idle and no successor playback during a 125-second observation window;
it did not independently prove the remote queue was empty.

An earlier `add_to_queue` call returned but did not produce the requested
successor in that bounded run. Do not generalize one failure into unsupported
status. The installed library's queue readback failed with an HTTP error and
is not needed for the successful control route. Command acceptance remains
separate from observed effects.

These are community-library implementation methods, not an official Google
Python SDK. Google's [Cast messages](https://developers.google.com/cast/docs/media/messages)
define standard status fields but leave `customData` application-specific.
The [native queue integration plan](M2-NATIVE-QUEUE-PLAN.md) describes the selected
next work and the user-approved one-successor continuation/reconnect behavior.
The [native foreground runner](NATIVE-QUEUE-RUNNER.md) now implements this route
as an opt-in mode. [M2 is closed with a known reliability limitation](M2-ACCEPTANCE.md);
an intermittent acknowledged enqueue failed to advance and remains tracked in
[issue #42](https://github.com/lbliii/tellyq/issues/42).
The [response-capture investigation](ENQUEUE-INVESTIGATION.md)
compares repeated and distinct third clips and records upstream parser/event-reader
options, including casttube's open message-log proposal and pyytlounge's existing
subscription implementation. No library upgrade or delivery fix has been inferred
from those sources.

## Possible later components

- [Home Assistant Universal Media Player](https://www.home-assistant.io/integrations/universal/): combines controls/status from multiple integrations; useful for coordinating TV, streaming device, and sound system.
- [Home Assistant Android TV Remote](https://www.home-assistant.io/integrations/androidtv_remote/): supports buttons, text, app launch and deep links on compatible Android/Google TV hardware. Documents missing playback status and Netflix command limitations. Not applicable to an older cast-only Chromecast.
- [Home Assistant ADB](https://www.home-assistant.io/integrations/androidtv/): Android-device control and app-dependent playback-state inference; requires device setup/pairing.
- [androidtvremote2](https://github.com/tronikos/androidtvremote2): direct remote protocol library. Its audio-command feature was researched but is not the selected route.
- [Google Home OK Google action](https://developers.home.google.com/automations/schema/reference/entity/assistant/ok_google_command): structured text-command action targeting a compatible speaker. Exact Netflix playback on this setup has not been tested.
- [Google Home Android Automation APIs](https://developers.home.google.com/apis/android/automation/build): authorized apps can create and execute automations. This requires an app integration, not merely an unauthenticated HTTP call.
- [Google Assistant SDK via Home Assistant](https://www.home-assistant.io/integrations/google_assistant_sdk/): explicitly documents that ordinary media-playback commands do not work. Do not confuse it with the newer Google Home automation path.
- [Google video control documentation](https://support.google.com/googlehome/answer/7214982?co=GENIE.Platform%3DAndroid&hl=en): service/device-specific support; series tend to resume and exact episode/season voice requests are not generally supported.
- [Smartest TV](https://github.com/Hybirdss/smartest-tv): candidate content resolver and agent interface. README says Samsung/Roku/Android drivers need real-world tests. Earlier source inspection found limited Samsung status and a queue action that did not establish continuous automatic advancement. Reinspect current code before relying on these features.
- [Sonos AI connector](https://support.sonos.com/en/article/control-your-sonos-system-with-ai-using-sonos-27mcp): documented music, volume, grouping and home-theater audio controls; not proof of Netflix title launching.

## Hardware distinction

[ARC](https://www.samsung.com/sg/support/tv-audio-video/how-to-use-hdmi-arc-on-samsung-smart-tv/) carries TV audio back to Sonos. [CEC/Anynet+](https://www.samsung.com/sg/support/tv-audio-video/use-anynetplus-hdmi-cec-on-your-samsung-smart-tv/) carries device-control commands and is not exclusive to the ARC port. [libCEC](https://github.com/Pulse-Eight/libcec) is a possible later power/input-control component. It does not by itself provide cross-service title selection or episode-progress information.

## Streaming control comparison, 2026-09-21

**Finding:** common Cast hardware does not mean a common exact-title playback API.
Google's [Cast architecture](https://developers.google.com/cast/docs/overview)
distinguishes ordinary media receivers from applications with authentication and
rights-management needs. Its [receiver example](https://developers.google.com/cast/codelabs/cast-receiver)
shows service-specific content IDs being resolved through the receiver's own
backend. A watch-page URL or receiver app ID is therefore not enough evidence that
TellyQ can launch a subscription title.

| Service | Documented route | What TellyQ actually knows | Planning decision |
| --- | --- | --- | --- |
| YouTube | PyChromecast YouTubeController with video ID | Start, identity, progress, visible playback and app-exit stop passed on this hardware; natural completion is untested | Continue here for queue/lifecycle engineering |
| Netflix | Official mobile casting is restricted by device and plan; native Google TV remote can launch deep links | On the Projector Google TV, software opened Netflix; after refreshed sign-in and profile selection, the exact-title link landed on Netflix home instead of the requested movie. Passive Cast status supplied no title or playback state | [Bounded probe](M4-NETFLIX-PROBE.md) classifies this route as assisted app launch; compare other services |
| Plex | A configured Plex Media Server supports exact library searches, receiver commands, current-item metadata, progress and controls; Home Assistant documents direct Plex-to-Cast playback | The user installed and signed in to the Google TV client; a fresh deep-link launch verified `com.plexapp.android`. No server was discoverable on the LAN. The official server archive passed its published checksum but macOS rejected the unpacked app, so it was not run | [Checkpoint](M4-PLEX-PROBE.md) selects Plex as the next candidate, pending trusted server install and a two-item test library |
| Disney+ | Official Android/iOS app supports Chromecast casting | Consumer casting documented; independent software launch and telemetry unknown | Compare with Prime Video using the same capability checklist |
| Prime Video | Official iOS/Android app supports Chromecast; Google TV devices can also run the app | Consumer casting documented; independent software launch and telemetry unknown | Compare with Disney+; no evidence yet that it is easier |
| Apple TV app | Apple's Android app documents Cast playback | A consumer Cast route exists; independent Mac/Python title launch and telemetry unknown | Keep in comparison; do not assume the Apple TV app requires an Apple TV hardware box |

Service sources, checked during planning:

- [Netflix supported casting devices and plans](https://help.netflix.com/en/node/100131): lists older Chromecast models without a remote (third generation or earlier), Nest Hub and selected TV models; Standard/Premium plans are required. Confirm the actual receiver and account tier before testing. This describes the Netflix mobile app, not an open Python control API.
- [Disney+ casting instructions](https://help.disneyplus.com/en-GB/article/disneyplus-en-uk-cast-airplay-tv): the official help article describes selecting content and a Chromecast in the Disney+ mobile app. The search index exposed the article text; direct page extraction was empty during this review. Recheck device/region-specific details before implementation.
- [Amazon's Chromecast instructions](https://digprjsurvey.amazon.co.uk/csad/help/node/G7U9H58SPSH7ZV4V): describes casting from current iOS/Android Prime Video apps and using the native app on Chromecast with Google TV. The main Prime Video help endpoint was inaccessible to the research tool; this is Amazon's own help copy.
- [Apple TV playback on Android](https://support.apple.com/en-euro/guide/tvapp-android/dev1a32599b3/web): documents selecting a Cast destination and stopping casting. Support in the app does not prove external automation access.
- [Plex Media Server integration](https://www.home-assistant.io/integrations/plex): documents exact movie and episode lookup, client playback, current-item metadata, progress and controls for supported clients.
- [Plex through Google Cast](https://www.home-assistant.io/integrations/cast#plex): documents direct playback from a configured Plex server to a Cast target.
- [Plex Companion support](https://support.plex.tv/articles/203082707-supported-plex-companion-apps/): classifies Android TV and Chromecast as receivers. A server, account and accessible library remain separate requirements.

### Device and transport routes

- **Existing Chromecast/Cast:** lowest setup cost and the proven YouTube route.
  Confirm generation before assuming it can run native Android apps or accept ADB.
- **Android/Google TV remote and deep links:** a candidate only on compatible
  hardware. [Home Assistant's Android TV Remote documentation](https://www.home-assistant.io/integrations/androidtv_remote/)
  states that playback status is unavailable through that API and lists Netflix
  command limitations. Opening a deep link can still land on a title page instead
  of playing an exact episode; observe the actual outcome.
- **Samsung native apps:** a separate device adapter, not the Chromecast protocol.
  The living-room model remains unverified. Launch, pairing and observations need
  their own experiment; the discovered desk monitor was not the living-room TV.
- **Google Home/Assistant integration:** the user's working Assistant setup is a
  lead. Google documents [script-editor text commands](https://developers.home.google.com/automations/schema/reference/entity/assistant/ok_google_command)
  and separately [Android Home automation creation/execution](https://developers.home.google.com/apis/android/automation/build).
  Neither document establishes that the desired Netflix command is exposed to this
  local Python controller. Confirm access, auth and supported commands; do not
  assume the script-editor action exists through the Android API. No synthetic
  spoken commands are part of the proposed route.

### Experiment record and selection rule

Use one row per **service + device model + control route + account context**, with
software versions and a last-tested date. For each capability record:

`unknown | documented | verified | unsupported`, its evidence source, and any
requirements. A service logo and a successful app launch never mark the row done.

Test exact title/episode launch, receiver identity, state, advancing position,
completion, stop, session takeover and restart. Record authentication/setup cost
and whether user interaction is required for every launch or only initial setup.
No credentials belong in the committed matrix.

Prefer a route that provides exact software launch **and** sufficient observations
to meet the requested autonomy. Verified launch/stop without completion can support
assisted viewing; it does not qualify for unattended advancement. If all candidate
routes fail a hard requirement, stop the probe and make the constraint explicit.

**Current decision:** YouTube remains the proven provider. The Netflix probe
confirmed assisted app launch without exact-title playback evidence. Plex is the
next candidate because its server and client model exposes exact library lookup
and playback state. The [Plex checkpoint](M4-PLEX-PROBE.md) has now verified the
signed-in native TV client, but found no server and remains at that trusted-install
setup gate.
Disney+ and Prime Video remain comparison fallbacks if Plex's local-library model
does not fit the desired catalog. See M4/M5 in [ROADMAP.md](ROADMAP.md).
