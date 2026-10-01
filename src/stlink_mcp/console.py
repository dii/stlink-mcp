"""Capturing the target's serial console.

Most ST development boards expose a UART through the on-board debug probe as a
USB CDC device. On a target whose debug port is closed in its normal boot
configuration, this is the only way to observe a running program, so it is a
first-class part of the tool surface rather than a convenience.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import serial

#: How long the port must stay quiet after opening before the capture counts
#: what arrives as new. Measured on an STLINK-V3E (2026-10-01): the output it
#: buffered while the port was closed, up to about 8 KB, arrived at once after
#: the open, and a target printing a line every 650 ms sent its next one then.
_BACKLOG_QUIET = 0.1

#: The longest the drain runs. A target that prints with no 100 ms gap keeps
#: it going, and what it prints in that time is set aside with the backlog.
_BACKLOG_LIMIT = 1.0


class ConsoleError(RuntimeError):
    pass


# There was a find_console_port() here, which returned STLINK_MCP_CONSOLE or else
# the first /dev/ttyACM* on the machine. run_firmware fell back to it whenever it
# had no probe listing, which is the guess docs/findings.md records as the wrong
# board about half the time with two attached. The console now comes only from
# the probe list or from STLINK_MCP_CONSOLE naming the probe (settings.py).


class ConsoleCapture:
    """Collects console output in the background across an operation.

    Used as a context manager around a load-and-run so that output produced
    immediately at start-up is not missed, which is exactly the output that says
    whether the thing started.

    What arrives the moment the port opens is kept apart, in `buffered`. An
    ST-LINK keeps what the target prints while nothing has its serial port open
    and hands it over on the next open, so those bytes come from before the
    capture: from the previous image, on the run path. Counting them as new is
    how a run of an image that printed nothing was verified on the previous
    image's lines (docs/findings.md, 2026-10-01). Clearing the host's input
    buffer does not catch them, because they have not arrived yet when it runs.
    """

    def __init__(
        self, port: str, baud: int = 115200,
        opener: Callable[..., Any] = serial.Serial,
    ) -> None:
        self.port = port
        self.baud = baud
        self._opener = opener
        self._serial: Any = None
        self._buffer = bytearray()
        self._buffered = bytearray()

    def __enter__(self) -> "ConsoleCapture":
        try:
            self._serial = self._opener(self.port, self.baud, timeout=0.1)
        except (serial.SerialException, OSError) as exc:
            raise ConsoleError(
                f"Could not open {self.port} at {self.baud}: {exc}. "
                "On Linux the user usually needs to be in the 'dialout' group."
            ) from exc
        self._serial.reset_input_buffer()
        try:
            self._drain_backlog()
        except (serial.SerialException, OSError) as exc:
            self.__exit__()
            raise ConsoleError(f"{self.port} failed while it was being opened: {exc}") from exc
        return self

    def _drain_backlog(self) -> None:
        """Set aside what arrives until the port has been quiet for _BACKLOG_QUIET."""
        start = last = time.monotonic()
        while True:
            now = time.monotonic()
            if now - start >= _BACKLOG_LIMIT or now - last >= _BACKLOG_QUIET:
                return
            waiting = self._serial.in_waiting
            if waiting:
                self._buffered.extend(self._serial.read(waiting))
                last = time.monotonic()
            else:
                time.sleep(0.005)

    def __exit__(self, *exc_info: object) -> None:
        if self._serial is not None:
            self._serial.close()
            self._serial = None

    def collect(self, seconds: float) -> str:
        """Read for a fixed period and return what arrived."""
        if self._serial is None:
            raise ConsoleError("Capture is not open")

        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            waiting = self._serial.in_waiting
            chunk = self._serial.read(waiting or 1)
            if chunk:
                self._buffer.extend(chunk)
        return self.text

    @property
    def text(self) -> str:
        """What arrived after the port went quiet, which is what a capture counts."""
        return self._buffer.decode("utf-8", errors="replace")

    @property
    def buffered(self) -> str:
        """What the probe handed over when the port opened, from before the capture."""
        return self._buffered.decode("utf-8", errors="replace")


# There was a one-shot capture() here. capture_console used it, and an unplug
# mid-capture escaped it as a SerialException with everything already read lost.
# capture_console now drives ConsoleCapture itself, as run_firmware does, so it
# keeps what arrived, and nothing else called this.
