# Manual test plan

This is the smoke-test checklist for verifying a release works end-to-end against a live mower.

Run these in order before tagging a release.

## Setup

- Lymow One mower with a known dock IP and at least one configured zone.
- HA dev install with `custom_components/lymow_mqtt/` symlinked or copied.
- Logging enabled in `configuration.yaml`:
  ```yaml
  logger:
    default: warning
    logs:
      custom_components.lymow_mqtt: debug
  ```

## Config flow

- [ ] **Native (SRP) sign-in** — region picker shows all 4 regions; email/password form accepts valid creds; bad password shows a clear error; device picker lists your mower(s).
- [ ] **Federated (OAuth) sign-in** — clickable link in the form opens the hosted UI; pasting the full `myapp://callback/?code=...` URL works; pasting just the bare code works; expired/invalid code shows a clear error.
- [ ] **Reauth** — manually corrupt `entry.data["refresh_token"]` to force a refresh failure; HA shows the reauth banner; clicking it routes to the right step (SRP or OAuth) based on the original method; on success, the existing config entry is updated (NOT a new entry created).

## Initial state

- [ ] Within ~5s of integration setup, primary sensors populate (battery, work_status as a label string, current_zone, error_message, online).
- [ ] `binary_sensor.online` reflects REST `/get-device-info` `deviceState`.
- [ ] `camera.lymow_mqtt_<sn>` has a `stream_source` and the stream loads in HA.
- [ ] `sensor.current_zone` exposes `available_zones` in its attributes — list of zones with `name`, `hash_id`, `mow_order`, `is_enabled`.

## Sensor labels & categorization

- [ ] **`work_status` shows a friendly label string** like "Mowing", "Docking", "Paused", "Paused (docking)" — NOT raw ints.
- [ ] **`robot_status` shows the same label vocabulary** (e.g. shows "Error" or "Emergency stop" when those states fire).
- [ ] **Cut height, cut speed, move speed** appear under the device's "Diagnostic" section, not the main sensor list. May show as Unknown (known issue — see README).
- [ ] **Default dashboard view** shows only primary sensors (battery, work_status, robot_status, current_zone, task_progress, mow_time, next_schedule, error_message, last_mow_duration).

## Lawn mower entity activity (priority matrix)

Verify the activity reflects the **combined** workStatus + robotStatus, not just workStatus:

- [ ] Mowing normally → activity: **mowing**
- [ ] Pause from MOWING → activity: **paused**, `work_status` label "Paused"
- [ ] Resume → activity: **mowing**
- [ ] Dock in HA (lawn_mower entity action) → activity: **returning** (transit), then **docked** (when robotStatus flips to Charging — even though workStatus may still be Docking)
- [ ] Mid-task recharge (mower auto-docks low-battery) → activity: **returning** during transit, **docked** during charging — verify card switches to "Docked" once at the dock
- [ ] Trigger a real error during a mow (lift the mower, simulated blade jam, etc.) → activity: **error**, `robot_status` shows "Error", `error_message` populates with a friendly label, `error_code` shows the int
- [ ] Lift the mower off the dock → activity: **error**, `robot_status` shows "Emergency stop", `warning_code` shows 4 with label "tip_over". Reset; verify all clear.
- [ ] Firmware updating (workStatus 11) — activity: **Unknown** (none of the 5 buckets fit cleanly)

## Live mowing

- [ ] Start a mow via the official app — within ~30s, `work_status` flips to "Mowing" and `current_zone` populates with the zone name (not hashId).
- [ ] `current_zone` updates as the mower moves between zones (via pose-in-polygon).
- [ ] Pause in HA — mower physically pauses within ~3s; `work_status` flips to "Paused".
- [ ] Resume in HA (the start button on lawn_mower entity, since HA's lawn_mower has no native resume) — mower physically resumes; `work_status` flips to "Mowing".
- [ ] Dock in HA (lawn_mower entity Dock button) → sends `RECHARGE_DOCK` (userCtrl=33) — mower drives home; **task progress is preserved** (verifiable in app: should still see "in progress").
- [ ] Manually start a fresh mow after the above dock → task resumes from where it paused.

## Edge cases

- [ ] Pause from DOCKING — works; `work_status` flips to "Paused (docking)".
- [ ] Resume from PAUSE_DOCKING — works.
- [ ] Try to send a command invalid for the current state (e.g. RESUME while idle) — service call returns a clear error in HA's UI.
- [ ] Multi-zone task — single `cleanReport` fires at end of all zones (per arch.md §11), not one per zone.

## Catalog refresh triggers

Verify the integration refreshes the zone catalog (and thus zone names + runtime config) at the right moments:

- [ ] **At integration startup** — within ~5s, `current_zone.attributes.available_zones` populates with the zone catalog.
- [ ] **On workStatus 1→2 transition** (Waiting → Mowing) — log shows `Refiring QUERY_MAP (transition=True, ...)`.
- [ ] **On cleanReport arrival** (mow completes) — log shows `Refiring QUERY_MAP (cleanReport=True, ...)`.
- [ ] **Rename a zone in the Lymow app, then start a fresh mow** — `available_zones` reflects the new name within ~30s of the workStatus 1→2 transition.

## Multi-zone start service

- [ ] Call `lymow_mqtt.start_zones` from Developer Tools → Services. Use the **Targets** section to pick the device, then in the data field provide:
  ```yaml
  zones:
    - "Pool"
    - "Front yard"
  ```
  Only those zones should be mowed, in the requested order.
- [ ] Same service with **hashIds** instead of names — works.
- [ ] Same service with a **mix of names and hashIds** in one call — works.
- [ ] Same service with a **single zone name as a scalar** (`zones: Pool` not `zones: [Pool]`) — works (auto-coerced to one-element list).
- [ ] Same service with an **unknown name** — service call fails with `HomeAssistantError("Unknown zone: 'Foo'. Known zones: Front yard, Pool, ...")`.
- [ ] Service call with **no device targeted** — fails with `HomeAssistantError("No Lymow device targeted. ...")`.

## Service: dock_cancel_task

- [ ] During an active mow, call `lymow_mqtt.dock_cancel_task` (with device targeted in the Targets section). Mower drives home AND task progress is **abandoned** (verify in the app — no resume option).

## Service: cancel_task

- [ ] During an error state (or any active task), call `lymow_mqtt.cancel_task`. Mower stops in place, `work_status` flips to "Waiting".

## Online detection

- [ ] Power down the mower — within 15min, `binary_sensor.online` flips false (REST poll detects deviceState=offline).
- [ ] Power back up — `online` flips true within 15min OR within seconds if a `/notify-app` MQTT message arrives.
- [ ] **Conflict resolution test:** while mower is broadcasting state via MQTT but REST is somehow stale-offline, `online` should still show true (MQTT activity within 5min overrides stale REST per spec §7.3).

## Multi-mower

- [ ] Add a second mower from the same account → separate device entry, independent state, services target each via device picker.

## Logs sanity check

After the full test pass, the log should contain:
- [ ] `MQTT subscribed mid=...` (subscribe ACK confirmed — not just connect)
- [ ] No `Refiring QUERY_MAP` retries beyond ~3 (ideally 0-1; >3 means the catalog is consistently incomplete and runtime_config is missing — known issue)
- [ ] No "command silently rejected" warnings unless you intentionally triggered an invalid command
- [ ] No exception tracebacks from `lymow_mqtt.*`

## Known issues to verify (NOT fix)

These are documented in the README; just confirm the symptoms match expectations rather than something worse:

- [ ] `cut_height`, `cut_speed`, `move_speed` may show "Unknown" — passive broadcasts don't carry runtime_config; only QUERY_MAP responses include it, and not always reliably.
- [ ] After renaming a zone in the Lymow app, the new name appears in HA only after the next workStatus 1→2 transition or cleanReport (not instantly).
- [ ] Federated sign-in requires a manual paste step every config flow / reauth — Cognito constraint, can't be eliminated client-side.
