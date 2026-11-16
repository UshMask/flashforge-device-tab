# FlashForge Device Tab

Read-only FlashForge monitoring inside OrcaSlicer's Device tab.

OrcaSlicer leaves the Device tab empty for FlashForge printers — the page fails
to load because the printer has no web UI of its own. This plugin fills the tab
with a local page served from inside OrcaSlicer: temperatures, print progress,
remaining time, layer, filament, the printer's file list with thumbnails, a
camera stream, a 30-minute temperature chart and a print history.

It needs no second application, no Node, no Docker and no Raspberry Pi. It runs
inside the plugin sandbox of stock OrcaSlicer.

## What it shows

- Nozzle and bed temperatures with targets
- Print progress, remaining and elapsed time, current layer
- Filament estimate for the running job
- Printer file list with PNG previews, search and pagination
- MJPEG camera stream (address is taken from the printer or entered by hand)
- Temperature chart for the last 30 minutes
- Print history with results, kept between sessions

## What it does not do

- No control: the plugin never starts, pauses or cancels a job.
- No print-host replacement: OrcaSlicer keeps sending jobs the way it always did.
- No other vendors: this is a FlashForge panel, not a universal monitor.
- No cloud, no accounts, no notifications.

If you need pause/cancel, multiple printers or Spoolman integration, use
[FlashForgeWebUI](https://github.com/Parallel-7/FlashForgeWebUI) instead — it is
a separate server with a much wider feature set.

## Requirements

- OrcaSlicer 2.5.x with the plugin system (tested on 2.5.1, Windows).
- FlashForge printer reachable over the LAN. Developed against Adventurer 5M;
  the same protocol is used by 5M Pro, AD5X and Creator 5.
- CheckCode from the printer's network settings (needed for the remaining-time
  estimate, filament and camera address).

## Install (local plugin)

1. Copy this folder so that the plugin file ends up here:

   ```
   %APPDATA%\OrcaSlicer\orca_plugins\flashforge_device_tab\flashforge_device_tab.py
   ```

2. In OrcaSlicer open **Plugins**, press **Refresh**, enable **FlashForge Device
   Tab** and allow the network access prompt.
3. Open the Device tab → **FlashForge Device Tab** → **Settings** and enter the
   printer IPv4 address and CheckCode. (The same fields exist in the Plugins
   dialog, but the panel's own form is enough.)
4. Open **Speed Dial** in the Device tab and choose **FlashForge Device Tab**.
5. The panel appears in the Device tab. The **Printer Web Interface** button
   returns to the page OrcaSlicer had there before.

If an older `printer_monitor` or `flashforge_monitor` installation is still
enabled, disable it first: two monitors polling the same printer fight over its
single TCP connection.

## Display modes

The panel's **Settings** button offers two layouts:

- **One printer** — the Device tab shows the printer of the active OrcaSlicer
  profile, with its full panel.
- **All printers** — the Device tab shows a dashboard at `/dashboard/` with a
  card per FlashForge printer profile.

Printer addresses and CheckCodes come from the printer profiles themselves
(`print_host` and `printhost_apikey`), so each printer keeps its own settings.
Changing the address in the panel also updates the active profile, because
OrcaSlicer sends jobs to the profile's `print_host`.

## How it works

The plugin starts a loopback HTTP server on `127.0.0.1:18771`, polls the printer
over TCP (`8899`, `~M105/M27/M119/M115/M661/M662`) and over its HTTP API
(`8898`, `/detail`), and serves the panel from that loopback address. Switching
the Device tab to the panel is done by writing `print_host_webui` in the active
printer profile; the previous value is saved per profile and restored by the
button.

Network access to the printer is granted by OrcaSlicer's plugin permission
prompt the first time the plugin connects to a new address.

## Development

```bash
python -m unittest discover -s tests -v
```

The suite runs the plugin against a fixture printer that speaks the same TCP and
HTTP dialects, so it covers the protocol, the history state machine, the HTTP
guards, the upload pause and the Device-tab switch without a real machine.

Repository layout:

```
flashforge_device_tab.py   the whole plugin (single file, stdlib only)
tests/test_plugin.py       fixture printer + 26 self-checks
```

## Card text (for the plugin catalog)

> OrcaSlicer leaves the Device tab empty for FlashForge printers. This plugin
> fills it: temperatures, print progress, remaining time, layer, filament used,
> the printer's file list with thumbnails, camera stream, a 30-minute
> temperature chart and a print history — all inside the tab. Read-only by
> design: it never sends, starts or cancels jobs, and needs no extra software.

## Credits

The FlashForge LAN protocol was reverse engineered by the community; this plugin
builds on what is documented in
[flashforge-python-api](https://github.com/GhostTypes/ff-5mp-api-py) and
[flashforge-finder-api](https://github.com/01F0/flashforge-finder-api).

Community plugin, not affiliated with FlashForge or OrcaSlicer.

## License

MIT — see [LICENSE](LICENSE).
