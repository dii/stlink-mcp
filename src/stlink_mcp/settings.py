"""Environment settings: read where they are used, checked once at start-up.

Two of these used to be parsed at import time, as module constants in server.py,
so `STLINK_MCP_GDB_PORT=6123x` killed the process with a traceback from inside
`from .server import ...`, before main() could turn it into the one-line message
and exit 2 that a bad board file gets. The same shape of failure
`_default_boards_path` was already written to prevent. main() now calls check()
first, and the tools read the values when they need them.

STLINK_MCP_CONSOLE changed meaning as well as place. It used to be one global
device path, consulted by capture_console only when the probe list mapped no
console to the probe, and ignored by run_firmware entirely, so a board wired to
a separate USB serial adapter could never get a verified run. It now takes a
device per probe serial, and a bare path only while a single probe is attached,
because one path applied to several boards is the wrong-board failure the
console mapping exists to prevent.
"""

from __future__ import annotations

import os

GDB_PORT = "STLINK_MCP_GDB_PORT"
CONSOLE_BAUD = "STLINK_MCP_CONSOLE_BAUD"
CONSOLE = "STLINK_MCP_CONSOLE"


class SettingError(ValueError):
    """An environment setting this server reads cannot be used, and says why."""


def _whole_number(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SettingError(f"{name} is {raw!r}, which is not a whole number.") from exc
    if not low <= value <= high:
        raise SettingError(f"{name} is {value}, outside {low} to {high}.")
    return value


def gdb_port_base() -> int:
    """The first GDB server port. The ceiling leaves room above it for the 64
    port pairs, four apart, that server._gdb_port_for steps through."""
    return _whole_number(GDB_PORT, 61234, 1024, 65000)


def console_baud() -> int:
    return _whole_number(CONSOLE_BAUD, 115200, 1, 20_000_000)


def _parse_console(raw: str) -> tuple[str | None, dict[str, str]]:
    """(bare device path, {probe serial casefolded: device path})."""
    value = raw.strip()
    if not value:
        return None, {}
    if "=" not in value:
        if "," in value:
            raise SettingError(
                f"{CONSOLE} is {raw!r}. Give one device path, or SERIAL=path pairs "
                "separated by commas."
            )
        return value, {}

    pairs: dict[str, str] = {}
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        serial, sep, path = (part.strip() for part in item.partition("="))
        if not sep or not serial or not path:
            raise SettingError(
                f"{CONSOLE}: {item!r} is not SERIAL=path. Mixing a bare path with "
                "pairs is not accepted either, since the bare path would have no "
                "probe to belong to."
            )
        if serial.casefold() in pairs:
            raise SettingError(f"{CONSOLE} names probe {serial} twice.")
        pairs[serial.casefold()] = path
    return None, pairs


def console_for(serial: str, attached: int) -> tuple[str | None, str | None]:
    """(device, note): the console STLINK_MCP_CONSOLE assigns to one probe.

    `attached` is how many probes are attached, since a bare path is only
    unambiguous while there is one. The note says why a bare path was not
    applied, so the setting is never silently out of effect.
    """
    bare, pairs = _parse_console(os.environ.get(CONSOLE, ""))
    if serial.casefold() in pairs:
        return pairs[serial.casefold()], None
    if bare is None:
        return None, None
    if attached == 1:
        return bare, None
    return None, (
        f"{CONSOLE} is the single device path {bare!r} and {attached} probes are "
        "attached, so it was applied to none of them. Name the probe it belongs "
        f"to, as {CONSOLE}=<probe serial>=<device path>."
    )


def console_is_bare_path() -> bool:
    """Whether STLINK_MCP_CONSOLE is a single device path rather than pairs."""
    bare, _ = _parse_console(os.environ.get(CONSOLE, ""))
    return bare is not None


def check() -> None:
    """Raise SettingError for the first setting that cannot be used."""
    gdb_port_base()
    console_baud()
    _parse_console(os.environ.get(CONSOLE, ""))
    # S1: a wrong install directory used to start a server whose every tool
    # then failed. Unset, the usual places are searched when a tool needs them.
    if os.environ.get("STLINK_MCP_CLT_ROOT"):
        from .toolchain import ToolchainError, find_toolchain
        try:
            find_toolchain().check()
        except ToolchainError as exc:
            raise SettingError(str(exc)) from None
