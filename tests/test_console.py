"""ConsoleCapture against a serial port that hands bytes over at set times.

An ST-LINK keeps what the target prints while nothing has its serial port open,
and hands it over in one burst just after the next open (measured on an
STLINK-V3E, 2026-10-01: 0.1 ms after the open). Clearing the host's input buffer
at open does not catch it, because it has not arrived yet. These tests give the
fake port that shape: some bytes already on the host when it opens, a backlog a
few milliseconds later, and live output after a gap.
"""

from __future__ import annotations

import time

import pytest

from stlink_mcp import console


class _ScheduledSerial:
    """Bytes become readable at given times after the port is opened."""

    def __init__(self, schedule: list[tuple[float, bytes]], already_here: bytes = b"") -> None:
        self._t0 = time.monotonic()
        self._schedule = sorted(schedule)
        self._next = 0
        self._pending = bytearray(already_here)

    def _pump(self) -> None:
        now = time.monotonic() - self._t0
        while self._next < len(self._schedule) and self._schedule[self._next][0] <= now:
            self._pending.extend(self._schedule[self._next][1])
            self._next += 1

    @property
    def in_waiting(self) -> int:
        self._pump()
        return len(self._pending)

    def read(self, n: int = 1) -> bytes:
        self._pump()
        if not self._pending:
            time.sleep(0.01)  # a read with nothing waiting blocks until its timeout
            self._pump()
        out = bytes(self._pending[:n])
        del self._pending[:n]
        return out

    def reset_input_buffer(self) -> None:
        self._pump()
        self._pending.clear()

    def close(self) -> None:
        pass


def _capture(schedule, already_here=b"") -> console.ConsoleCapture:
    port = _ScheduledSerial(schedule, already_here)
    return console.ConsoleCapture("/dev/fake", 115200, opener=lambda *a, **k: port)


BACKLOG = b"previous image tick 41\r\nprevious image tick 42\r\n"


class TestTheProbesBacklogIsKeptApart:
    def test_a_backlog_at_open_is_not_what_the_capture_counts(self) -> None:
        with _capture([(0.01, BACKLOG)]) as cap:
            text = cap.collect(0.2)
        assert text == ""
        assert cap.buffered == BACKLOG.decode()

    def test_output_after_the_quiet_gap_is_counted_and_the_backlog_is_not(self) -> None:
        live = b"new image tick 0\r\n"
        with _capture([(0.01, BACKLOG), (0.4, live)]) as cap:
            text = cap.collect(0.4)
        assert text == live.decode()
        assert cap.buffered == BACKLOG.decode()

    def test_bytes_already_on_the_host_at_open_are_discarded(self) -> None:
        with _capture([], already_here=b"stale host bytes") as cap:
            text = cap.collect(0.05)
        assert text == "" and cap.buffered == ""

    def test_with_no_backlog_nothing_is_set_aside(self) -> None:
        live = b"tick 0\r\n"
        with _capture([(0.2, live)]) as cap:
            text = cap.collect(0.3)
        assert cap.buffered == ""
        assert text == live.decode()

    def test_a_target_that_never_pauses_is_set_aside_only_up_to_the_limit(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(console, "_BACKLOG_LIMIT", 0.2)
        chatter = [(0.01 + i * 0.02, b"x") for i in range(40)]  # no 100 ms gap for 0.8 s
        start = time.monotonic()
        with _capture(chatter) as cap:
            assert time.monotonic() - start < 0.5, "the drain must stop at its limit"
            text = cap.collect(0.7)
        assert cap.buffered and text, "the chatter is split between the two, not lost"
        assert len(cap.buffered) + len(text) == 40


def test_a_port_that_fails_while_draining_is_a_console_error() -> None:
    class _Dies(_ScheduledSerial):
        @property
        def in_waiting(self) -> int:
            raise OSError("device disconnected")

    port = _Dies([])
    with pytest.raises(console.ConsoleError):
        with console.ConsoleCapture("/dev/fake", 115200, opener=lambda *a, **k: port):
            pass
