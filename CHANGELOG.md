# Changelog

## 0.21.3.post9 - 2026-08-27

- Ignore state-changing LastData echoes during direct on-demand sessions and
  use the independently polled LightLevel response as the sole state source.
- Prevent firmware 6.43.3's reproducible stale `off` then `on` echo sequence
  from creating false light transitions in Home Assistant and bridge clients.
- Expose filtered direct state echoes in diagnostics and cover the exact stale
  target-node echo with a regression test.

## 0.21.3.post8 - 2026-08-27

- Bind notification handlers to the exact BLE client and Plejd node that
  created them so delayed packets from an old direct session are discarded.
- In direct on-demand mode, accept state updates only for outputs owned by the
  connected target hardware and ignore stale broadcast or cross-node updates.
- Expose discarded stale and cross-node notifications in diagnostics and add
  regression coverage for rapid consecutive commands to different nodes.

## 0.21.3.post7 - 2026-08-22

- Add an opt-in on-demand direct-control mode that disconnects from the target
  node after each confirmed command while preserving Home Assistant
  availability.
- Mark only the hardware behind a fresh Plejd advertisement available and mark
  a direct target unavailable when its BLE connection cannot be established.
- Retry a failed on-demand write against the same target rather than falling
  back to an unrelated persistent gateway.
- Expose on-demand disconnects in non-secret reliability diagnostics.

## 0.21.3.post6 - 2026-08-22

- Route address-specific control writes through a direct authenticated BLE
  connection to the Plejd hardware that owns the target output.
- Keep the selected target connected for later commands, avoiding reconnects
  for repeated dim updates to the same light.
- Allow an explicit direct control route to override the persistent gateway
  blacklist without changing the saved gateway preferences.
- Expose direct-route, gateway-switch, and route-failure counters in diagnostics.
- Retain the post5 physical-state/reconnect path for commands that cannot be
  mapped safely to one hardware node.

## 0.21.3.post5 - 2026-08-22

- Never treat a `LastChangedDataVector` control echo as proof that a physical
  Plejd output changed state.
- Poll `NodeIndexData` after non-gateway writes and accept only that independent
  state report as confirmation.
- Require both power state and raw dim level to match for dimming commands
  before skipping recovery.
- Keep in-place reauthentication and a full reconnect as the two fallback
  stages when the polled physical state does not match.

## 0.21.3.post4 - 2026-08-21

- Try a fresh Plejd authentication challenge on the existing BLE connection
  after an unconfirmed mesh command.
- Poll and accept a matching state update after in-place reauthentication.
- Preserve the full disconnect/reconnect as the final fallback and expose
  reauthentication attempt/success counters in diagnostics.

## 0.21.3.post3 - 2026-08-21

- Wait up to 1.5 seconds for a matching state confirmation after a command to
  a non-gateway node.
- Skip the reconnect when that confirmation arrives; retain the proven
  reconnect-and-flush fallback only for unconfirmed commands.
- Count confirmations and timeouts in the non-secret reliability diagnostics.

## 0.21.3.post2 - 2026-08-21

- Authenticate the first BLE connection directly and retain the five-second
  double-connect workaround only as a fallback.
- Validate the Bleak client's real connection state and serialize connection
  and command recovery.
- Retry a failed GATT write once after a full reconnect.
- Optionally reconnect after control writes to non-gateway mesh nodes so Plejd
  firmware 6.43.x flushes queued commands.
- Make button-event polling optional and expose non-secret reliability counters.
- Declare the already-used `cryptography` package as a direct dependency.
- Preserve Home Assistant availability during the intentional command-flush
  reconnect, while real transport failures still mark devices unavailable.
