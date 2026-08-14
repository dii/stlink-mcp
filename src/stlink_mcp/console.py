"""Capturing the target's serial console.

Most ST development boards expose a UART through the on-board debug probe as a
USB CDC device. On a target whose debug port is closed in its normal boot
configuration, this is the only way to observe a running program, so it is a
first-class part of the tool surface rather than a convenience.
"""

from __future__ import annotations

import glob
import os
import time

import serial


class ConsoleError(RuntimeError):
    pass


def find_console_port() -> str | None:
    """Guess the probe's virtual COM port.

    Deliberately returns None rather than raising: a target with no console is a
    normal situation, not an error.
    """
    override = os.environ.get("STLINK_MCP_CONSOLE")
    if override:
        return override

    for pattern in ("/dev/ttyACM*", "/dev/tty.usbmodem*", "/dev/ttyUSB*"):
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[0]
    return None


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


def capture(port: str, seconds: float, baud: int = 115200) -> str:
    """One-shot capture, for when there is nothing to run alongside it."""
    with ConsoleCapture(port, baud) as cap:
        return cap.collect(seconds)
