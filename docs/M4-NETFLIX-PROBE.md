# M4 Netflix feasibility probe

This is one bounded investigation of Netflix on the existing Projector
Chromecast. It is not a TellyQ playback adapter or an M5 acceptance run.

## Device and route

On 2026-09-22, a read-only local device-info request reported Google as the
manufacturer, `Chromecast` as the model name, and `sabrina` as the product name.
That product code identifies the [Chromecast with Google TV (4K)](https://github.com/oddsolutions/sabrina-unlock).
The Android TV
Remote service accepted a one-time local pairing through `androidtvremote2`
0.3.2. Pairing certificate and key, device address, and probe scripts remain
under ignored `runtime/netflix-probe/`; no account credentials were requested
or stored.

Netflix's [current casting help](https://help.netflix.com/en/node/100131)
limits phone casting to listed devices and excludes this Google TV model. The
native Google TV app and its local remote protocol are the route for this
probe. Home Assistant documents that [Android TV Remote can launch deep
links](https://www.home-assistant.io/integrations/androidtv_remote/), while its
API does not report playback status and its key commands do not work in
Netflix. Google documents that Assistant can open Netflix or request a show,
but [specific seasons and episodes are unsupported](https://support.google.com/chromecast/answer/10115165?hl=en).

## Observed so far

The local remote endpoint was reachable; ADB TCP was unavailable. After
pairing, the remote reported the Google TV launcher as the active app. Sending
`netflix://` changed its active-app report to `com.netflix.ninja`. A separate
passive Cast status read named Netflix as the app but returned `UNKNOWN` media
state and no content ID. The user separately saw Netflix's profile-selection
screen. This proves software app launch, not title selection or playback;
profile choice currently requires a user action.

## Remaining bounded check

The user selected the 2025 film *Anaconda* for this test. Sending its exact
Netflix `title/82018723` link kept the Netflix app active but showed a generic
"Something went wrong" screen without an error code. Sending the matching
`watch/82018723` link in the same session opened Netflix's profile selector.
The passive Cast read still reported `UNKNOWN` media state, no content ID and
zero position. The remote reported only the Netflix app. Neither command
establishes title playback or progress.

When the user attempted profile entry, Netflix displayed `tvq-prfls-101`.
[Netflix's help page](https://help.netflix.com/en/node/51959) says this code
usually means stored device data needs refreshing. This prevents attribution
of the title-link failure to the remote route itself: the Netflix app could
not complete profile entry during the session. We did not sign out, reset the
app, or handle account credentials. A local `HOME` key command returned the
remote's active-app report to the Google TV launcher.

| Capability | Result on this device and account |
| --- | --- |
| Software app launch | Verified by remote app report and user-visible Netflix profile screen |
| Exact title launch | Unknown; the title link errored and the direct-watch link reached profile selection before `tvq-prfls-101` |
| Title identity and playback state | Unavailable in passive Cast/remote telemetry in this session |
| Progress and completion | Unverified |
| Stop or leave app | `HOME` returned to the Google TV launcher; stopping active Netflix playback was not tested |
| Authentication/setup | One-time remote pairing succeeded; Netflix profile entry blocked by app error |

**Decision:** blocked by the Netflix app's profile error for this bounded
session. After the user restores normal Netflix profile entry using Netflix's
published steps, one fresh exact-title check could distinguish a working
deep link from app-open-only control. Even if launch succeeds, the missing
playback telemetry means this route cannot yet support unattended advancement.
Compare Disney+ and Prime Video before implementing a subscription adapter.
