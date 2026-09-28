"""Capturing the target's serial console.

Most ST development boards expose a UART through the on-board debug probe as a
USB CDC device. On a target whose debug port is closed in its normal boot
configuration, this is the only way to observe a running program, so it is a
first-class part of the tool surface rather than a convenience.
"""

from __future__ import annotations

import time

import serial


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
    """

    def __init__(self, port: str, baud: int = 115200) -> None:
        self.port = port
        self.baud = baud
        self._serial: serial.Serial | None = None
        self._buffer = bytearray()

    def __enter__(self) -> "ConsoleCapture":
        try:
            self._serial = serial.Serial(self.port, self.baud, timeout=0.1)
        except (serial.SerialException, OSError) as exc:
            raise ConsoleError(
                f"Could not open {self.port} at {self.baud}: {exc}. "
                "On Linux the user usually needs to be in the 'dialout' group."
            ) from exc
        # Discard anything buffered from before this run.
        self._serial.reset_input_buffer()
        return self

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
        return self._buffer.decode("utf-8", errors="replace")


# There was a one-shot capture() here. capture_console used it, and an unplug
# mid-capture escaped it as a SerialException with everything already read lost.
# capture_console now drives ConsoleCapture itself, as run_firmware does, so it
# keeps what arrived, and nothing else called this.
