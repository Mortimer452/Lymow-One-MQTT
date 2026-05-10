# Lymow One MQTT

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/release/Mortimer452/Lymow-One-MQTT.svg)](https://github.com/Mortimer452/Lymow-One-MQTT/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20me%20a%20coffee-support-yellow?logo=buy-me-a-coffee)](https://buymeacoffee.com/mortimer452)

Home Assistant integration for the **Lymow One** robotic lawn mower.

Communicates with the mower over AWS IoT MQTT (the same channel the official Lymow app uses), so it works over both Wifi and 4G and sees everything the official app does so nothing stays out of sync.
Whatever you do in the app, this HA integration will see it, too, most sensors update immediately.

The integration is strictly-passive by design, just listens to messages sent by the mower. Message updates (battery level, time elapsed, percent complete, current zone, etc) occur roughly every 30-60 seconds during mowing, but much slower during charging (5-15 minutes)

> **Status:** v0.3.0 — adds per-zone last-mowed sensors for time-based mow automations. Tested on Lymow One. **Lymow One Plus** is expected to work but is unverified — please open an issue if you have one.

## Features

- **Lawn mower entity** with Start / Pause / Dock controls. 
- **Device tracker integration** with live GPS coordinates showing the position of mower and RTK station in HA's map
- **Live state sensors:** battery, work status, current zone (derived from mower position within map polygons), task progress, error messages.
- **Multi-zone start service** for kicking off mows on a specific list of zones.
- **Read-only schedule sensor** with the next upcoming run plus all schedules in attributes.
- **RTSP camera** entity streaming the mower's onboard camera over your LAN.
- **Diagnostic sensors:** RTK quality, signal strength, firmware version, IP address, last-mow summary, error codes.
- **Per-zone sensors:** Tracks last mow time per zone. Sensor attributes track mow count by zone, time spent, zone area.
- **Push-driven** — no chattery polling for state, ~15-min REST poll only for online/offline detection.

## Install (HACS custom repository)

1. Open HACS → **Custom repositories** → **+**
2. Repository: `https://github.com/Mortimer452/Lymow-One-MQTT`
3. Type: **Integration**
4. Click **Add**
5. Search "Lymow" in HACS, install
6. Restart Home Assistant
7. **Settings → Devices & services → + Add integration → Lymow One MQTT**

## Install (manual)

Copy `custom_components/lymow_mqtt/` into your HA config directory, restart HA, then add the integration via Settings → Devices & services.

## Configuration

Prett self-explanatory, the config flow walks you through it:

1. **Region** — Pick your AWS region, Lymow has four, your account is region-locked to one of them, just pick the one closest to you
2. **Sign-in method:**
   - **Email + password** (Lymow account) — enter creds to login.
   - **Sign in with Google or Apple** (OAuth) — Works, but very fiddley as Lymo's OAth config is locked down to a specific URL. Needs a one-time browser copy-paste step. Click the link the integration shows you, **be sure to hit F12 to open browser dev tools**, sign in, and your browser will fail to redirect to `myapp://callback/...` — that's expected. Copy the URL from your address bar (but ONLY if it starts with myapp://) or find the failed redirect to myapp:// in your dev tools window and paste it back into HA.
3. **Mower** — pick which Lymow mower to add (one config entry per mower if you have multiple).

## Lawn Mower Entity

Uses HA's built-in Lawn Mower Entity type. The entity only has three commands - Start, Dock, Pause. This integration maps these HA commands to the correct Lymow commands based on the Lymow's state:

- Start: Starts a full mow using default order. When paused or docked for recharge-and-resume, resumes the task. Not available while mowing or in error condition.
- Pause: Issues a pause command. Also clears an error condition if the mower is in error. Not available while docked.
- Dock: Return to the dock. If mowing, saves the session for recharge-and-resume.

## RTSP camera

The mower exposes its onboard camera as `rtsp://<mower-lan-ip>:10022/h264ESVideoTest`. The integration creates a `camera` entity using this URL automatically. 

- A **DHCP reservation** for the mower in your router is recommended. Integration will refresh the stream source if the IP changes, but won't be instant. 

## Services

- `lymow_mqtt.start_zones` — start a mow on a specified list of zone hashIds or names in order.
- `lymow_mqtt.dock_cancel_task` — dock the mower **and cancel** the current task.
- `lymow_mqtt.cancel_task` — force-reinit the mower (stop in place, reset to waiting). Equivalent to "Cancel task" in app. Also clears error if present.

The standard `Dock` action on the lawn_mower entity behaves the same as tapping Dock and choosing to KEEP progress

## Caveats

- **Federated sign-in requires a manual paste step** every config flow / reauth. We tried to find a redirect URI that would let us auto-capture the code — Cognito only accepts `myapp://callback/` for this client, so manual paste is unavoidable.
- The integration is independent from the `d3dfantasy99/Lymow-HA` integration, no name conflicts so you can run both at the same time if desired

## Known issues

- **Cut height, cut speed, and move speed sensors update inconsistently.** Values may stay at "Unknown" or remain stale until the next time you start a mow or pause/resume. This is a trade-off of the strict-passive design and may be revisited in a future version.

## Support

- Issues: https://github.com/Mortimer452/Lymow-One-MQTT/issues
- Buy me a coffee: https://buymeacoffee.com/mortimer452

## License

MIT.
