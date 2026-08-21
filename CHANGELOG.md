# Changelog

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
