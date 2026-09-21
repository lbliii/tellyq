# Research handoff

Checked 2026-09-21. These are documented capabilities and proposed approaches, not hardware-test results.

## First prototype

- [PyChromecast](https://github.com/home-assistant-libs/pychromecast): open-source Python library for Cast discovery, app channels, media control, and status. Current README requires Python 3.11+. Network discovery uses mDNS; the local host needs a reachable network path to the Cast device.
- [YouTube controller](https://github.com/home-assistant-libs/pychromecast/blob/master/pychromecast/controllers/youtube.py): exposes `play_video`, `add_to_queue`, and `play_next`; internally uses casttube. Do not treat a YouTube watch URL as a directly playable media file URL.
- [YouTube example](https://github.com/home-assistant-libs/pychromecast/blob/master/examples/youtube_example.py): shows discovery by device name, handler registration, and playback by video ID.
- [Open compatibility proposal, PR #1155](https://github.com/home-assistant-libs/pychromecast/pull/1155): reports a YouTube lounge API HTTP 400 problem and proposes setting Content-Length. Author reports a Nest Hub test; this is not validation on the user's Chromecast.
- [Bob Ross — Autumn Fantasy](https://www.youtube.com/watch?v=FozIp7Va7dY): official-channel full episode, season 20, episode 7. Availability on the user's playback device remains to be tested.

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
