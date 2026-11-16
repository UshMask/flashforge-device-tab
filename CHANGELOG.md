# Changelog

## 1.4.0

Settings for the two things users asked about: what is read from profiles, and
whether the extra tab exists.

### Added

- **“Read CheckCode from printer profiles”** (on by default). Turning it off keeps
  the plugin from reading `printhost_apikey`; a code typed in the panel is then
  stored in the plugin config instead.
- **“Show the Printers tab”** (on by default). Turning it off leaves the tab in
  place with a short note instead of the dashboard.
- Both switches are in the panel's Settings and in the dashboard's Settings.

### Fixed

- The Device tab no longer stays broken after an upgrade: a `print_host_webui`
  that points at an older plugin port (18765/18769) is rewritten to the current
  page on plugin load, and the previous value is kept for the “back to native
  page” action.

## 1.3.1

Fixes the empty Printers tab.

### Fixed

- The Printers tab renders the same loopback dashboard in an iframe instead of
  relying on the page-to-plugin message bridge, so it works even when the bridge
  is not available.
- Printer discovery no longer depends on a single preset API: named lookup,
  indexed lookup and the merged `full_config_value()` of the active profile are
  combined, so the active printer is always listed even if the collection cannot
  be read from the plugin thread.
- Added `GET /dashboard/debug` returning what discovery sees, for support.

## 1.3.0

The dashboard becomes its own OrcaSlicer tab, and coexistence with other printers
is explicit.

### Added

- **A separate “Printers” tab** (plugin `Pages` capability) that is always
  available, whatever printer profile is active. The Device tab keeps its
  per-profile page.
- **A setting for the Device tab**: keep the printer panel (default) or replace it
  with the dashboard for FlashForge profiles.
- The dashboard lists **every printer profile**: FlashForge printers with live
  data, printers with their own web interface (link to it), and printers without
  one (marked as not monitored), plus a short note about the limitation.

### Changed

- Display mode values are now `panel` (default) and `device`; the previous
  `profile`/`dashboard` values are still accepted.
- Only FlashForge profiles are ever rewritten; other vendors keep their own
  Device tab page untouched.

## 1.2.0

Two display modes, chosen by the user, and real multi-printer support.

### Added

- **Display mode.** The panel's Settings dialog and the dashboard both offer
  *one printer (the active OrcaSlicer profile)* or *all printers at once*. The
  choice is stored in the plugin config and rewrites `print_host_webui`, so the
  Device tab shows the page that matches the mode.
- **Dashboard at `/dashboard/`**: a card per FlashForge printer profile with
  state, progress, remaining time, temperatures and a link to its full panel.
- **Printer discovery from OrcaSlicer profiles.** Every printer profile with
  host type FlashForge contributes its address (`print_host`) and CheckCode
  (`printhost_apikey`), so each printer keeps its own settings and no second
  list has to be maintained.
- Per-printer monitors and history files; the active printer is polled every
  5 s, the others every 15 s.

### Changed

- The panel now serves any discovered printer at `/<address>/`, not only the
  configured one.
- `POST /settings` accepts a `mode` field and applies the mode together with the
  address and CheckCode.

### Fixed

- The panel no longer refuses to open when the active profile's address differs
  from the last saved plugin config; the profile is the source of truth.

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
