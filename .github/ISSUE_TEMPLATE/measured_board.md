---
name: I ran a part this project has not measured
about: Values for a device ID, and how each one was established
labels: measurement
---

**Device ID and board**
The device ID as `board_state` reports it, and the board it was run on.

**Values, and how each was established**

| field | value | how it was established |
| --- | --- | --- |
| access_port | | |
| ram_load_base | | |
| vector_table_addr | | |
| flash_base | | |

For example: "ran a RAM image and read its counter advancing", "console output
from the new image", or "from the reference manual, not run".

**What was run**
The image, how it was linked, and what showed it started.

**Probe firmware and STM32CubeCLT version**
