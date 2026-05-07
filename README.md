# 🌿 Lymow Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/release/d3dfantasy99/Lymow-HA.svg)](https://github.com/d3dfantasy99/Lymow-HA/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Discord](https://img.shields.io/discord/rPyv8mcB?label=Discord&logo=discord)](https://discord.gg/rPyv8mcB)
[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20me%20a%20coffee-support-yellow?logo=buy-me-a-coffee)](https://buymeacoffee.com/d3dfantasy99)

Unofficial Home Assistant integration for the **Lymow robot lawn mower**.  
Control your robot, monitor its status, view zones and map — all from Home Assistant.

> ⚠️ **This integration is not affiliated with or endorsed by Lymow.**  
> It was built by reverse engineering the official Lymow Android app.

---

## Features

- 🤖 **Lawn Mower entity** — start, pause, dock via standard HA lawn mower card
- 🔋 **Sensors** — battery, work status, blade height, mow mode, RTK GPS, WiFi/4G signal, firmware version, session area, mow duration
- 🟢 **Binary sensors** — online, charging, mowing, error, rain delay, WiFi/4G connected
- 🗺️ **Map camera** — live SVG map rendered from zone and obstacle data with robot position, RTK and battery HUD
- 🎛️ **Controls** — blade height slider (20–60 mm), mow mode selector
- 📅 **Services** — start specific zones, set blade height, configure weekly schedule
- 🔄 **Multi-region** — Europe, Asia Pacific (Sydney & Hong Kong), US East
- 🔁 **Token auto-refresh** — stays logged in, no manual intervention needed

---

## Requirements

- Home Assistant **2024.1** or newer
- A Lymow account created with **email and password**
- Your robot must be paired to that account via the official Lymow app

> ⚠️ **Google and Apple login are not supported.**  
> Those accounts use OAuth2 with a mobile deep link (`myapp://callback`) that cannot be replicated in a headless environment.  
> Please create a dedicated Lymow account with email and password and pair your robot to it.

---

## Installation

### Via HACS (recommended)

1. Make sure [HACS](https://hacs.xyz) is installed in your Home Assistant instance.
2. Click the button below to add this repository to HACS:

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=d3dfantasy99&repository=Lymow-HA&category=integration)

Or manually:
- Go to **HACS → Integrations → ⋮ → Custom repositories**
- Add `https://github.com/d3dfantasy99/Lymow-HA` as an **Integration**
- Search for **Lymow** and click **Download**

3. Restart Home Assistant.

### Manual installation

1. Download the [latest release](https://github.com/d3dfantasy99/Lymow-HA/releases/latest).
2. Copy the `custom_components/lymow` folder into your HA `config/custom_components/` directory.
3. Restart Home Assistant.

---

## Setup

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **Lymow**
3. Enter your email, password and select the AWS region closest to you:

| Region | Use if you are in |
|--------|------------------|
| Europe (Ireland) | Europe |
| Asia Pacific (Sydney) | Australia, Oceania |
| Asia Pacific (Hong Kong) | Asia |
| US East (Ohio) | Americas |

4. If multiple robots are found, select which one to add.
5. Done — entities will appear under the Lymow device.

> You can add the integration multiple times to manage multiple robots.

---

## Entities

| Entity | Type | Description |
|--------|------|-------------|
| Lymow Robot | `lawn_mower` | Main control entity |
| Battery | `sensor` | Battery level % |
| Status | `sensor` | Work status (Mowing, Docked, Charging…) |
| Mow Mode | `sensor` / `select` | Current cutting pattern |
| Blade Height | `sensor` / `number` | Cutting height in mm |
| RTK GPS | `sensor` | GPS fix quality (Not Ready / Float / Fixed) |
| Session Area | `sensor` | Area mowed in current session (m²) |
| WiFi Signal | `sensor` | WiFi RSSI (dBm) |
| 4G Signal | `sensor` | LTE RSSI (dBm) |
| Firmware | `sensor` | Current firmware version |
| Online | `binary_sensor` | Robot connectivity |
| Charging | `binary_sensor` | Whether robot is charging |
| Mowing | `binary_sensor` | Whether robot is actively mowing |
| Error | `binary_sensor` | Whether an error is active |
| Map | `camera` | SVG map with zones, obstacles and robot position |

---

## Services

### `lymow.start_zone`
Start mowing one or more specific zones.

```yaml
service: lymow.start_zone
data:
  zone_ids:
    - "zone_hash_id_1"
    - "zone_hash_id_2"
```

### `lymow.set_blade_height`
Set the cutting blade height (20–60 mm, step 5).

```yaml
service: lymow.set_blade_height
data:
  height_mm: 40
```

### `lymow.set_schedule`
Configure the weekly mowing schedule.

```yaml
service: lymow.set_schedule
data:
  schedules:
    - day: 1        # 0=Sun, 1=Mon … 6=Sat
      startHour: 9
      startMin: 0
      duration: 120  # minutes
    - day: 4
      startHour: 10
      startMin: 0
      duration: 90
```

> If you have more than one robot, add `entry_id: <config_entry_id>` to target a specific one.

---

## Troubleshooting

### Enable debug logging

Add the following to your `configuration.yaml` and restart Home Assistant:

```yaml
logger:
  default: warning
  logs:
    custom_components.lymow: debug
```

Logs will appear in **Settings → System → Logs**. They include the full shadow state payload from the robot, which is useful for diagnosing missing or incorrect sensor values.

### Common issues

**Integration not found after installation**  
→ Make sure you restarted Home Assistant after copying the files.

**Login fails**  
→ Confirm you are using an account created with **email and password**, not Google or Apple.  
→ Try logging in with the same credentials in the official Lymow app to verify they are correct.

**All sensors unavailable**  
→ The robot may be offline or out of WiFi/4G range. Check the **Online** binary sensor.  
→ Enable debug logging and check for shadow fetch errors.

**Map is empty**  
→ The robot needs to have completed at least one mapping session. The map is fetched from the shadow and backup S3 data — it may take one polling cycle to appear.

---

## Support

Join the community Discord server for help, feedback and discussion:

[![Discord](https://img.shields.io/badge/Discord-Join%20Server-5865F2?logo=discord&logoColor=white)](https://discord.gg/rPyv8mcB)

If you find this integration useful and want to support its development, you can buy me a coffee:

[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20me%20a%20coffee-support-yellow?logo=buy-me-a-coffee)](https://buymeacoffee.com/d3dfantasy99)

To report a bug or request a feature, please [open an issue](https://github.com/d3dfantasy99/Lymow-HA/issues) on GitHub.

---

## Disclaimer

This integration communicates directly with Lymow's AWS infrastructure (Cognito, API Gateway, IoT Shadow) using credentials obtained by reverse engineering the official Android app. Use at your own risk. The API may change at any time without notice.
