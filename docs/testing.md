# Manual test plan

This is the smoke-test checklist for verifying a release works end-to-end against a live mower.

Run these in order before tagging a release.

## Setup

- Lymow One mower with a known dock IP and at least one configured zone.
- HA dev install with `custom_components/lymow_mqtt/` symlinked or copied.
- Logging enabled: `logger: { default: warning, logs: { custom_components.lymow_mqtt: debug } }`

## Config flow

- [ ] **Native (SRP) sign-in** — region picker shows all 4 regions; email/password form accepts valid creds; bad password shows a clear error; device picker lists your mower(s).
- [ ] **Federated (OAuth) sign-in** — clickable link in the form opens the hosted UI; pasting the full `myapp://callback/?code=...` URL works; pasting just the bare code works; expired/invalid code shows a clear error.
- [ ] **Reauth** — manually corrupt `entry.data["refresh_token"]` to force a refresh failure; HA shows the reauth banner; clicking it routes to the right step (SRP or OAuth) based on the original method.

## Initial state

- [ ] Within ~5s of integration setup, all primary sensors populate.
- [ ] Online binary_sensor reflects REST `/get-device-info` `deviceState`.
- [ ] Camera entity has a `stream_source` and the stream loads in HA.

## Live mowing

- [ ] Start a mow via the official app — within ~30s, work_status flips to `2` and current_zone populates.
- [ ] Pause in HA — mower physically pauses within ~3s; work_status flips to `3`.
- [ ] Resume in HA — mower physically resumes; work_status flips to `2`.
- [ ] Dock in HA (lawn_mower entity action) — mower drives home; task remains preserved (verifiable in app).
- [ ] Resume task after recharge — works.

## Edge cases

- [ ] Pause from DOCKING — works; work_status flips to `10` (PAUSE_DOCKING).
- [ ] Resume from PAUSE_DOCKING — works.
- [ ] Try to send a command that's invalid for the current state (e.g. RESUME while idle) — service call returns clear error.
- [ ] Trigger a real error during a mow (lift the mower, simulated blade jam, etc.) — error_active flips true, error_message populates with a friendly label, error_code shows the int.
- [ ] Lift the mower off the dock — emergency_stop flips true, warning_code shows 4 with label "tip_over". Reset and verify both clear.

## Multi-zone

- [ ] Call `lymow_mqtt.start_zones` with 2 zone hashIds — only those zones are mowed, in the requested order.

## Online detection

- [ ] Power down the mower — within 15min, online binary_sensor flips false.
- [ ] Power back up — online flips true within 15min OR within seconds if a `/notify-app` message arrives.

## Multi-mower

- [ ] Add a second mower from the same account — separate device entry with independent state.
