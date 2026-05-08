# Lymow One MQTT

Home Assistant integration for the **Lymow One** robotic lawn mower.

Communicates with the mower over AWS IoT MQTT (the same channel the official Lymow app uses), so it works with both BLE-out-of-range mowers and home installations far from the dock.

> **Status:** v0.1.0 — first release. Tested on Lymow One. **Lymow One Plus** is expected to work but is unverified — please open an issue if you have one.

## Features

- **Lawn mower entity** with Start / Pause / Dock controls. Pause/resume automatically pick the right firmware variant based on whether the mower is mowing or docking.
- **Live state sensors:** battery, work status, current zone (derived from mower position), task progress, error messages.
- **Multi-zone start service** for kicking off mows on a specific list of zones.
- **Read-only schedule sensor** with the next upcoming run plus all schedules in attributes.
- **RTSP camera** entity streaming the mower's onboard camera over your LAN.
- **Diagnostic sensors:** RTK quality, signal strength, firmware version, IP address, last-mow summary, error codes.
- **Push-driven** — no polling for state, ~15-min REST poll only for online/offline detection.

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

The config flow walks you through:

1. **Region** — pick your AWS region (us-east-2, eu-west-1, ap-southeast-2, ap-east-1).
2. **Sign-in method:**
   - **Email + password** (native account) — straightforward.
   - **Sign in with Google or Apple** (federated) — needs a one-time browser paste step. Click the link the integration shows you, sign in, and your browser will fail to redirect to `myapp://callback/...` — that's expected. Copy the URL from your address bar and paste it back into HA.
3. **Mower** — pick which Lymow mower to add (one config entry per mower if you have multiple).

## RTSP camera

The mower exposes its onboard camera as `rtsp://<mower-lan-ip>:10022/h264ESVideoTest`. The integration creates a `camera` entity using this URL automatically. **For the camera to work**:

- HA must be able to reach the mower's LAN IP (no IoT VLAN blocking, etc.).
- A **DHCP reservation** for the mower in your router is recommended — the integration will refresh `stream_source` if the IP changes, but a stable IP makes it more reliable.
- HA needs **go2rtc** or **ffmpeg** to decode the stream (HA's default `stream` integration handles this).

## Services

- `lymow_mqtt.start_zones` — start a mow on a specified list of zone hashIds in order.
- `lymow_mqtt.dock_cancel_task` — dock the mower **and abandon** the current task. Destructive.
- `lymow_mqtt.cancel_task` — force-reinit the mower (stop in place, reset to waiting). Use when stuck in error.

The standard `Dock` action on the lawn_mower entity sends `RECHARGE_DOCK`, which **preserves task progress** so you can resume later. That's the safer default.

## Caveats

- **Federated sign-in requires a manual paste step** every config flow / reauth. We tried to find a redirect URI that would let us auto-capture the code — Cognito only accepts `myapp://callback/` for this client, so manual paste is unavoidable.
- The integration is **strict-passive** by design: ~30-90s state update cadence during mowing, ~15min during charging. No real-time tracking. If you want sub-second updates, a future version may add an opt-in switch.
- The integration is independent from the upstream `d3dfantasy99/Lymow-HA` integration. **Uninstall that one first** before installing this — they share entity naming patterns at the device-registry level.

## Support

- Issues: https://github.com/Mortimer452/Lymow-One-MQTT/issues
- Buy me a coffee: https://buymeacoffee.com/mortimer452

## License

MIT.
