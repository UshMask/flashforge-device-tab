# Changelog

## 1.14.2

- Prevent two Windows instances from serving the FlashForge monitor on the same
  local port. Show the address and a restart hint in OrcaSlicer diagnostics
  when the port is unavailable.
- Enable the taller Print job card by default and let users switch it off in Monitor settings.
- Add a two-step first-printer wizard and let users change tab placement in Monitor settings.
  Use the built-in Printer tab by default.

## 1.14.1

- Connect to the built-in camera on both Creator 5 models even when the printer
  omits the stream URL. Identify the model by product ID and distinguish
  toolheads from material slots.
- Hide Home, jog, extrusion and error-clear controls on AD5X firmware older
  than 1.1.6.
- Show when a print has finished while the printer still reports it is busy.
  Prompt the operator to remove the print and dismiss the completion dialog.

## 1.14.0

- Mark completed jobs as waiting for part removal in All printers. Keep the
  state until the operator confirms the bed is clear. This can be turned off
  in All printers settings.
- Customize temperature plots in Monitor settings. Choose shared or separate
  scales and visible readings and targets. Remember the selected time range.
- Give Monitor, All printers, Printer Files, and Print History independent
  settings pages alongside Appearance and Connection.
- Combine print history across configured and archived printers. Filter by
  printer or open a printer's history directly from Monitor.
- Use the same compact job card in Monitor and All printers, with a model
  preview, camera controls and the print status together.

## 1.13.1

- Correct printer terminology across the translated interface, including axis movement, homing, and heating.
- Fix wording for print jobs, build plates, filament, and sliced .3mf files.
- Translate compact nozzle and bed readings, door status, and the message shown while waiting for readings.

## 1.13.0

- Add **Appearance** settings to the plugin panel. Choose the theme and layout
  density, then customize the Monitor, All printers, Printer Files and Print
  History blocks in their own subsections.
- Localize the monitor, printer list, history, settings and notifications.
  Follow OrcaSlicer's language by default or choose an interface language in Config.
- Combine printers and jobs in a compact **All printers** list. Search by
  printer or file and filter by state. Ready printers offer a shortcut to
  stored files and can repeat their own last completed job.
- Show the print file's preview beside the live camera when available. Camera
  playback in **All printers** can be turned off for the whole list.
- Add icons across the panel, show the chamber light state, keep Settings
  actions visible while scrolling, and compare file and slot colors before a reprint.
- Find modern FlashForge printers on the local network and test the connection
  from the settings form before saving.
- Switch the chamber light from the camera panel and edit reported materials
  and colors on idle AD5X and Creator 5 material stations. Read back the change
  from the printer before confirming it.
- Repeat a stored Creator print with material requirements read from its original
  sliced `.3mf`. Remember linked materials after the printer reports the start.
- Open the monitor in its own FlashForge tab by default. The plugin settings
  still offer the option to use the built-in Printer tab.
- Open the current job from the Monitor header to jump to its row in
  **All printers**. Print totals sit with the job history.
- Group XY, Z and filament controls. Use a clear Home icon and show a Home
  hint if the printer explicitly refuses an axis move because it needs homing.
  Show the nozzle heating requirement beside the filament buttons when needed.
- Show one nozzle for single-nozzle printers, including AD5X. On Creator 5
  models, highlight heads with a heating target and keep the other readings
  visible.
- Keep file cards readable when a preview is missing. After a refresh, newly
  detected files appear above previously seen ones.
- Upload a sliced file to the Adventurer 5M file list without starting a print.
  The plugin checks the file list and the printer's idle state after the transfer.

## 1.12.1

- Arrow keys jog only while the XY pad has focus. Shift increases the step
  from 1 mm to 10 mm. Enter activates Home only when its button has focus.
- Keep Manual control open when readings become unavailable or stale, with
  commands disabled until fresh readings arrive.
- Wrap long connection errors in narrow windows.

## 1.12.0

- Open the printer's web interface in the system browser and recover Device
  tabs left on a connection error by the previous switch behavior.
- Choose the built-in Device tab or a dedicated FlashForge tab in the plugin
  manager. OrcaSlicer applies tab visibility after a restart.
- Reorganized the monitor so the temperature chart and file catalog stay in
  place when Manual control opens on wider screens. Grouped XY, Z and filament
  controls and moved the connection status next to the printer readings.

## 1.11.0

- Added model-specific manual controls. Adventurer 3/4 and 5M-series printers
  use TCP for motion, while AD5X uses its HTTP motion commands. Creator 5 and
  Creator 5 Pro expose calibration and the firmware-reported shutdown timer.
  Available controls follow the detected printer and its current state.
- Adventurer 3/4: Home, XYZ jogging and filament feed/retraction. Adventurer
  5M/5M Pro also support leveling, input shaping and the reported shutdown timer.
- AD5X: Home, XYZ jogging, feed/retraction, leveling, input shaping, clearing
  an active error and the reported shutdown timer. Creator 5/5 Pro: leveling,
  input shaping and the reported timer. Manual commands require an idle printer.
- Tested with simulated printers. Physical verification of manual controls is pending.

## 1.10.0

- Added LAN camera viewing for Creator 5 Pro. Save the displayed frame as a
  JPEG during playback. Hiding or closing the monitor releases the camera.
  Returning reconnects until the user chooses **Disconnect**.
- Verified playback, snapshot continuity and reconnection with a simulated
  Creator 5 Pro in Chrome. Physical printer verification is pending.
- This stable release freezes the feature set and begins maintenance-only support.

## 1.9.1

- Pause, resume and cancel now report the observed printer state. If a transition
  takes time, the panel checks status again and sends the command only once.
- Switching between printer fields and bulk text preserves vertical bars in
  CheckCodes and names. Existing pipe-delimited rows remain supported.
- A late connection-check response can no longer reopen a dismissed dialog or
  replace a newer report.

## 1.9.0

- Navigate between Monitor, All printers and History. The job card shows the
  estimated finishing time. Connection status includes the age of the last
  reading and a check of the saved printer's telemetry and authentication.
- Filter the fleet for printers needing attention. Switch monitored devices
  within the active FlashForge profile. The visible printer gets faster polling.
- Inspect actual and target temperatures on the chart. Creator users can
  select T1–T4. Experimental AD5X support displays reported IFS slots, filament
  and the selected slot from status telemetry.
- Search history, filter errors and cancellations, and open a job for details.
  Observed ending times are recorded for new entries.
- Edit printer settings in individual fields or as a list. Camera and printer
  details remember their expanded state per device.
- Choose which host notifications to receive, including sustained loss of
  connection. Connection reports can be copied without network identifiers.

## 1.8.0

- Read legacy Adventurer-style TCP progress and inline temperature replies.
  Adventurer 3 response samples are covered by the local test suite.
- Preserve one history entry across a monitoring gap when the filename and
  elapsed time identify a continuing job.
- Bring the temperature chart and camera closer to the job. Printer details
  expand on demand, and the fleet view hides single-printer controls.
- Show the configured printer name in the header and hide job controls when
  authenticated status is unavailable. Refresh the panel icon and overview.

## 1.7.2

Privacy scrub of the published material.

- The in-app setup example and the README no longer show a real printer address
  or CheckCode; they use `192.168.1.50 | 1a2b3c4d`.
- The README screenshot is a synthetic demo capture, not a photo of a real
  printer session.

This release also brings the Creator 5 / 5 Pro support and the remembered
Device page from 1.7.1 to the catalogue.

## 1.7.1

### Fixed

- The Device tab opens the plugin panel automatically for configured FlashForge
  profiles after restarting OrcaSlicer or selecting a profile. The page choice is
  kept in plugin storage, so an unsaved printer preset cannot reset it.
- Choosing **Printer Web Interface** is remembered for that profile and address.
  The original native URL remains available when switching back.
- Page restoration runs on a background worker instead of blocking lifecycle
  callbacks on OrcaSlicer's UI thread.

## 1.7.0

Monitoring grows up, and the panel can now stop a job.

### Added

- **Pause, resume and cancel** from the panel header (FlashForge `/control`
  API). Cancel asks for a second click before it sends anything.
- **Printer card** — model, firmware, serial number, MAC, build volume and the
  light state, straight from `M115`/`M119`.
- **Totals** — jobs, print time and success rate from the local print history.
- **Save snapshot** — one JPEG frame from the camera, saved by the browser.
- **Host notifications** — “Print finished”, “Print cancelled” and “Printer
  error” in the 3D view, so a finished job is visible without watching the tab.

## 1.6.6

The release workflow uploads the plugin with the platform suffix the catalogue
requires (`_win_x86_64.py`).

## 1.6.5

- Publishing happens on a published GitHub release through
`publish-orcacloud.yml`, exactly as the catalogue documents it.

## 1.6.4

- One workflow now does everything on a `v*` tag: tests, the GitHub release and
the OrcaCloud publish call (OIDC, no tokens).

## 1.6.3

- The GitHub release now publishes straight to the catalogue through the
`publish-orcacloud.yml` workflow (OIDC trusted publishing, no tokens).

## 1.6.2

- Release check: publishing this tag only through GitHub, without uploading
the file to the catalogue by hand.

## 1.6.1

- The “All printers” view no longer leaves the temperature chart panel behind,
  and the header toggle no longer wraps onto two lines.
- Releases are published from GitHub: pushing a `v*` tag runs the tests and
  creates the release asset the OrcaSlicer catalogue picks up.

## 1.6.0

Several printers in one panel, and a setup screen instead of an empty tab.

### Added

- **Printers in the plugin config**: `address | CheckCode | name`, one per line in
  Settings. Every configured printer is monitored — the open one every five
  seconds, the rest every fifteen (automatic, depending on how many there are).
- **“All printers” toggle** in the panel header: a compact list of every printer
  with state, progress and remaining time; click one to open its full panel.
- **Setup screen**: when no printer is configured, the panel explains what to
  open and where to press instead of showing an empty grid.

### Changed

- The Device tab page now identifies printers by address from the plugin config;
  printer profiles are no longer read to discover printers.
- Settings dialog edits the printer list instead of a single address.

## 1.5.0

Back to a single printer panel, and back to a low risk rating.

### Changed

- Removed the experimental Printers tab and dashboard together with the profile
  discovery they needed. The plugin is again one panel in the Device tab,
  configured by printer address and CheckCode.
- The plugin no longer reads `printhost_apikey` from printer profiles. The
  CheckCode is stored in the plugin config, and the catalogue scan rates the
  plugin **low risk** again.
- On the first load after an upgrade the plugin adopts the address from the
  active printer profile when its own config has none.

### Kept

- The Settings dialog in the panel header (address, CheckCode).
- Redirects from the older loopback ports (18765/18769), so an upgraded Device
  tab never stays on a dead address.

## 1.4.0

Settings for the two things users asked about: what is read from profiles, and
whether the extra tab exists.

### Added

- **вЂњRead CheckCode from printer profilesвЂќ** (on by default). Turning it off keeps
  the plugin from reading `printhost_apikey`; a code typed in the panel is then
  stored in the plugin config instead.
- **вЂњShow the Printers tabвЂќ** (on by default). Turning it off leaves the tab in
  place with a short note instead of the dashboard.
- Both switches are in the panel's Settings and in the dashboard's Settings.

### Fixed

- The Device tab no longer stays broken after an upgrade: a `print_host_webui`
  that points at an older plugin port (18765/18769) is rewritten to the current
  page on plugin load, and the previous value is kept for the вЂњback to native
  pageвЂќ action.

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

- **A separate вЂњPrintersвЂќ tab** (plugin `Pages` capability) that is always
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
  strings (`"205 В°C"`); the page formats units itself. This removes the fragile
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

