# VicoHome for Home Assistant

**English** · [Italiano](README.it.md)

Custom integration for **VicoHome** cameras and doorbells (A4x/Addx platform), with a
**native WebRTC live view** in the Home Assistant player.

Status: **0.6.0, experimental**. Tested on 17/11/2026 with a DB2 doorbell (battery powered): live view with
audio, also with the doorbell on another network; recorded live views and clips archived locally.

## What it does

For every camera in the account (shared ones included):

| Entity | Source |
|---|---|
| Camera with **live view** (WebRTC) and still image | live: signaling through the cloud, video straight camera → browser; image: cloud cover or photo of the last event |
| Battery, Wi-Fi signal, IP, Wi-Fi network | `/device/listuserdevices` |
| Last event (AI tags, photo and HLS video in the attributes) | `/library/newselectlibrary` |
| Recent event (on for 2 minutes after an event), charging, online, awake | same |
| **Event** `event.*_activity`: `ring` (doorbell), `person`, `vehicle`, `pet`, `package`, `motion`, `live` (camera started, once the file is saved) | **instant FCM push** (measured ~1 s from the button press); as a fallback, new library items (30 s polling) |
| Dashboard card **VicoHome: events** (`custom:vicohome-events-card`): events and live views with photo, label and time, as a list or a grid grouped by day; filter by period (today, yesterday, last 24 hours, 7 or 30 days, a single day or a range from the calendar) and by type, "Load more" to go back in time; tap one to open the video in a dialog | websocket command `vicohome/events` (archive + cloud, URLs signed for 1 hour); the JS is served from `/vicohome_static/` and always loaded |
| **Local archive** (7 days by default, configurable) in `/config/vicohome/<entry>/`: every finished event is downloaded as MP4 + JPEG, and every live view watched is recorded | events: the library HLS remuxed by ffmpeg without re-encoding (the initial audio track stalls the player); live: ffmpeg reads the RTSP of the go2rtc bridge (video copied, audio to AAC) |
| **Media browser** → VicoHome → camera: local archive + cloud clips not downloaded yet (☁) | archive served by `/api/vicohome/archive/...`, cloud clips remuxed on the fly by `/api/vicohome/clip/...` |

Polling (every 30 s) only reads the cloud and **does not wake** battery cameras.
The live view does wake the camera: keeping it open drains the battery.

## How the live view works

To be able to record, the live view goes through a **private go2rtc** (the binary shipped with HA, started
by the integration on localhost with a STUN server; it does not touch the go2rtc managed by HA):

```
browser ──WebRTC──► go2rtc (ports 11985 API, 18564 RTSP, 18565 WebRTC) ──WebRTC/WHEP──► HA ──wss A4x──► cloud ──► camera
                       │
                       └──RTSP──► ffmpeg ──► /config/vicohome/<entry>/YYYYMMDD-hhmmss_<sn>_live.mp4
```

- go2rtc pulls the camera from `/api/vicohome/whep/<key>/...` (random key at every start): HA forwards
  the offer over the A4x signaling (compressed "KISS" SDP) and returns the camera's answer.
- The camera always answers audio = mid 0 and video = mid 1, while go2rtc offers video first: the offer
  is reordered and the answer mapped back to go2rtc's mids.
- A camera that is waking up may announce itself twice (`PEER_IN`): the offer is then sent again.
- When the live view closes, ffmpeg finalizes the file and the link to the camera is closed (it goes back to sleep).
- **Microphone on** (`sendrecv` audio with an SSRC in the browser's offer): the live view goes straight
  browser ↔ camera, without recording. The same direct path is the fallback if go2rtc does not start.
  Here HA only relays the signaling: audio is announced `sendrecv` with an SSRC and the camera's answer
  is reordered like the browser's offer.

## Push notifications

HA registers with Firebase Cloud Messaging using the public FCM identity of the Android app (as the Ring
integration does) and hands the token to the cloud with `/user/setusertoken` (`msgType` `"101"`).
VicoHome pushes (`type` `"3"` = doorbell, with `serialNumber` and `traceId`) fire the event entity at once;
the same `traceId` later comes from the library and is ignored.
The `firebase-messaging` client is extended because VicoHome pushes carry a `crypto-key` header with several
fields; a watchdog restarts it if it stops. FCM credentials are stored in `.storage/vicohome.<entry>.fcm`.

## Account

VicoHome allows **one session per account**: every login signs the same account out of the app.
Use a **secondary account** and share the cameras to it from the main app.

## Installation

**HACS**: HACS → ⋮ menu → Custom repositories → `https://github.com/maxeo/ha-vicohome`, category
Integration; then install **VicoHome** and restart HA.

**Manual**: copy `custom_components/vicohome` into `/config/custom_components/`, restart HA,
then Settings → Devices & services → Add integration → **VicoHome**.

Languages: **English, Italian and German** (setup, options, entities, media browser and events card);
other languages fall back to English. The card follows the user's profile language, the media browser
the server's.

**Options** (⚙ Configure on the integration):
- **What to record**: doorbell, person, vehicle, pet, package, motion, camera started (live view opened from HA)
  (multiple choice; without "camera started" the live view goes straight camera → browser, without go2rtc);
- **Retention**: from 1 day to 1 year, "Forever" (manual cleanup of `config/vicohome`) or
  "Custom…" (a second step asks for the number of days, up to 3650).

The events card is always available in "Add card" (**VicoHome: events**); add it by hand to the dashboard
you prefer. Configuration (all fields are optional):

```yaml
type: custom:vicohome-events-card
camera: camera.front_gate_doorbell             # omitted: all cameras
event_entity: event.front_gate_doorbell_activity  # refreshes at every new event
limit: 12                                      # events per page
layout: list                                   # list (grouped by day) or grid; the button at the top switches it
columns: 3                                     # grid only
title: Events
```

Options apply without reloading the integration: entities do not turn "unavailable" and the event entity
does not record the restored last event again.

The icon is in `custom_components/vicohome/brand/` (HA uses it for custom integrations); the vector
source is `docs/icon.svg`.

To understand the fields of a new model: integration menu → **Download diagnostics**
(cloud data with email, token, IP, MAC and URLs redacted).

## Thanks, credits and licenses

This integration would not exist without the work of those who studied VicoHome before me: a sincere
thank you to the authors of the projects below, from whom I learned the protocol and took inspiration.

- [averagenative/vicolocal](https://github.com/averagenative/vicolocal) (MIT): signaling protocol and
  SDP codec (`vico_sdp.py` copied unchanged, see `LICENSE.vicolocal`).
- [dydx/vico-cli](https://github.com/dydx/vico-cli) (MIT): login, device list, events.
- [TomTje/vicohome-ha](https://github.com/TomTje/vicohome-ha) (MIT): the first HA integration.

Not affiliated with VicoHome.

## To do

Endpoints found in the Android app (list in `docs/endpoints-app.txt`), request bodies still to be worked out:
- siren: `device/doalarm`, `device/cancelAlarm`;
- settings: `device/getuserconfig` / `device/updateuserconfig`, `device/getDeviceAttributes` /
  `device/modifyDeviceAttributes`, `device/updatepersondetect`;
- doorbell: `device/config/querydoorbellconfig` / `updatedoorbellconfig`, `device/mechanical/dingdong/audition`;
- light: `device/queryWhiteLight`, `device/updateFloodlightSwitch`, `device/updateFloodlightLuminance`;
- wake-up: `device/wakeupDevice`; pan/tilt: `device/rotate`, `device/rotateToPoint`.
