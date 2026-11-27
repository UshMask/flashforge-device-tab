Follow your FlashForge prints inside OrcaSlicer. The FlashForge tab shows progress, time remaining, nozzle and bed temperatures, printer files and supported manual controls. It also supports cameras on compatible models. The plugin connects to your printer over the local network.

## Monitor

See the current job, temperatures, files and available controls in one place. Pause, resume and cancel are available on supported printers.

![Monitor showing a print in progress, temperature chart and files](orca-cloud/01-monitor.png)

## All printers

See every configured printer in a vertical list. Search by printer or job, or filter by status. Active jobs show progress, a model preview and a live camera where available. You can turn off camera playback for the whole list. Open stored files or repeat the last completed job from a ready printer's row.

![All printers with printing, paused, idle and disconnected rows](orca-cloud/02-all-printers.png)

## History

See print times and outcomes, search for a file, or filter for errors and cancellations. The history is stored locally.

![History showing print totals, durations and outcomes](orca-cloud/03-history.png)

## Appearance

Choose the theme and layout density, then show or hide blocks for each view in Settings.

![Appearance settings showing theme, density and Monitor options](orca-cloud/06-appearance.png)

## Manual control

Supported idle printers show Home, XY and Z movement, filament controls or calibration, depending on the model.

![Manual control with Home, XY and Z movement, filament and a shutdown timer](orca-cloud/04-manual-control.png)

## Connect a printer

1. With the plugin enabled, open **FlashForge → Settings** in OrcaSlicer.
2. Click **Add printer**, enter the printer’s IPv4 address and CheckCode, then click **Save**. Creator 5 and 5 Pro also require the serial number.

Your printer and computer need to be on the same local network. For basic monitoring of Adventurer 5M and 5M Pro, the address is enough. The CheckCode enables additional readings and supported controls.

![Two steps to connect: open Settings, enter the printer details and save](orca-cloud/05-connect-printer.png)

Screenshots use demonstration data. Requires OrcaSlicer 2.5.0 or newer on Windows x64.

[Source, installation and model compatibility](https://github.com/UshMask/flashforge-device-tab)
