# Manual control visual check

Compared the selected AD5X reference with the running Device panel in Chrome.
Both show an idle printer, a 1 mm XY step, disabled error clearing and the shutdown timer set to Off.

The panel follows the reference order, including the model and status header,
split motion area, filament row, service buttons and shutdown row. The reference
icons are embedded in the plugin for offline use. The native timer selector
and browser font rendering have minor visual differences from the reference.

Verified the XY selector and jog, both bed directions, feed, leveling and timer
submission through the simulated printer. Checked side-by-side axis controls
and horizontal overflow at 320 and 375 CSS pixels. Physical printer testing
was outside this visual check.

final result: passed

## Print jobs camera grid

Compared the approved camera grid with the running plugin at the same desktop width. Both show each printer and its status above an aligned media area, followed by the file, progress and layer. The implementation retains the FlashForge navigation and uses contained camera frames so that the edges of the build plate stay visible. The simulator supplies JPEG test frames.

Verified simultaneous managed and direct camera streams, enlarged preview over the same managed connection, matching snapshot bytes, reconnection after returning to Print jobs, and the stored-file confirmation. Checked browser overflow at 320, 736 and 1024 CSS pixels. Physical camera testing was outside this check.

final result: passed
