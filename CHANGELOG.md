# Changelog

## 1.1.0

Settings without leaving the Device tab.

### Added

- **A Settings dialog inside the panel.** The header now has a `Settings` button
  that opens a small form with the printer IPv4 address and CheckCode. The
  values are read from the plugin config, validated and written back through
  `save_config`, so the Plugins dialog is only needed for the first install.
- `GET /<endpoint>/settings` returns the current address and code; `POST` with
  the `X-FlashForge-Tab-Action: settings` header applies them. Both are guarded
  by the same Host/Origin/`Sec-Fetch-Site` checks as the "back to native page"
  action, and the body is limited to 4 KB.
- Changing the address also updates the active printer profile
  (`print_host` and, when the Device tab shows this panel, `print_host_webui`),
  so sending files and the panel URL keep pointing at the new printer. The page
  then moves itself to the new URL.

### Changed

- The POST handler now routes by action header instead of assuming "restore",
  so the two write endpoints cannot be confused with each other.

## 1.0.0

First release of **FlashForge Device Tab**. This is the consolidated successor of
`flashforge_monitor` 1.5.0 and `printer_monitor` 0.2.2.

Scope is now fixed: FlashForge only, read-only, one plugin.

### Changed

- Renamed from `Printer Monitor` to `FlashForge Device Tab`; the plugin is
  FlashForge-only and no longer advertises universal printer support.
- **PrusaLink support removed.** OrcaSlicer supports PrusaLink natively, so the
  adapter duplicated built-in functionality while doubling the polling and
  routing code.
- Status API now returns numbers (`nozzle: 205.3`) instead of pre-formatted
  strings (`"205 °C"`); the page formats units itself. This removes the fragile
  regex parsing of display strings on the client.
- Loopback page moved to port `18771` so it cannot collide with an older
  installed monitor.

### Fixed

- The poll loop is wrapped so that one bad response, or an exception in history
  bookkeeping, can no longer stop the server thread and silently freeze the
  panel.
- A missing `UploadFinished` event no longer blocks FlashForge monitoring
  forever: a stale upload flag expires after 15 minutes.
- Upload start waits for an in-flight printer request for at most 1.5 s instead
  of an unbounded time, so "Send" cannot stall the UI for a whole poll cycle.
- Large file catalogs and previews no longer fail on slow links: the read
  timeout is now per-read (4 s idle) with a 30 s total cap, instead of a single
  3 s budget for the whole transfer.
- `PermissionError` is handled explicitly: if the plugin is not allowed to reach
  the printer, the panel says so instead of showing "no response" forever.
- Preview and file-list failures return 503 ("printer did not respond") instead
  of 409, which is reserved for "the open profile does not match the plugin
  settings".

### Removed

- `flashforge_monitor.py` is retired; its features are covered here.
- The multi-profile routing layer (per-profile monitors, `PRINTER_NAMES`,
  `printer_endpoint` branching) that only existed to serve PrusaLink.
