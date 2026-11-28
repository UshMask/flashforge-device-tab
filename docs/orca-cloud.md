Follow your FlashForge prints inside OrcaSlicer. The monitor shows progress, time remaining, nozzle and bed temperatures, printer files and supported manual controls. It also supports cameras on compatible models. The plugin connects to your printer over the local network.

## Monitor

See the current job, temperatures, files and available controls in one place. Choose shared or separate chart scales and which readings and targets to show. Pause, resume and cancel are available on supported printers.

![Monitor showing a print in progress, temperature chart and files](orca-cloud/01-monitor.png)

## All printers

See every configured printer in a vertical list. Search by printer or job, or filter by status. Active jobs show progress, a model preview and a live camera where available. Confirm that the bed is clear after a print, or turn off this reminder in settings. Open stored files or repeat the last completed job from a ready printer's row.

![All printers with printing, paused, idle and disconnected rows](orca-cloud/02-all-printers.png)

## History

See locally recorded print times and outcomes across configured and archived printers. Filter by printer, search for a file, or show errors and cancellations.

![History showing print totals, durations and outcomes](orca-cloud/03-history.png)

## Settings

Choose the theme and layout density in Appearance. In Monitor settings, choose the built-in Printer tab or a separate FlashForge tab. Restart OrcaSlicer to switch tabs. The taller Print job card is enabled by default and can be switched off.

![Monitor settings showing chart scales and temperature readings](orca-cloud/06-monitor-settings.png)

## Manual control

Supported idle printers show Home, XY and Z movement, filament controls or calibration, depending on the model.

![Manual control with Home, XY and Z movement, filament and a shutdown timer](orca-cloud/04-manual-control.png)

## Connect a printer

1. Open the built-in **Printer** tab with a FlashForge printer profile selected. If no FlashForge profile is selected, run **FlashForge Device Tab** from Speed Dial. Choose the tab you want to use in the setup wizard.
2. Find your printer or enter its IPv4 address, then check the connection and save. Creator 5 and 5 Pro require the serial number and CheckCode, with LAN-only mode enabled on the printer. Add more printers later in **Settings → Connection**.

Your printer and computer need to be on the same local network. For basic monitoring of Adventurer 5M and 5M Pro, the address is enough. The CheckCode enables additional readings and supported controls.

![Printer connection settings for adding more printers](orca-cloud/05-connect-printer.png)

Screenshots use demonstration data. Requires OrcaSlicer 2.5.0 or newer on Windows x64.

[Source, installation and model compatibility](https://github.com/UshMask/flashforge-device-tab)
