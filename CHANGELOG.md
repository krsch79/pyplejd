# Changelog

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
