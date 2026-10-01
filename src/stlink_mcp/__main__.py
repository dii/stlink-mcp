"""Entry point. Runs the MCP server on stdio, which is how MCP clients spawn it."""

from __future__ import annotations

import argparse
import signal
import sys

from . import __version__, settings
from . import server as server_mod
from .quirks import BoardConfigError
from .runner import release_all_servers
from .toolchain import ToolchainError


def _release_and_exit(signum: int, _frame: object) -> None:
    """Free held probes on a signal, which `atexit` alone does not cover.

    Python runs `atexit` handlers on a normal exit, and on SIGINT because it
    installs a handler for that one. It installs none for SIGTERM or SIGHUP, so
    the default action terminates the process immediately and any held GDB
    server is orphaned: it keeps the probe and both of its ports, and nothing on
    disk records that it exists. SIGTERM is what `kill` and any supervisor send,
    and SIGHUP is what a closing terminal sends, so neither is an exotic case.

    SIGKILL cannot be caught and is not covered. Nothing in a process can cover
    it, which is worth saying plainly rather than implying the guarantee is
    complete.
    """
    release_all_servers()
    # Die the way the signal would have, so a supervisor sees a normal signal
    # death rather than a clean exit that hides what happened.
    signal.signal(signum, signal.SIG_DFL)
    signal.raise_signal(signum)


def _install_signal_handlers() -> None:
    wanted = [signal.SIGTERM]
    hangup = getattr(signal, "SIGHUP", None)  # absent on Windows
    if hangup is not None:
        wanted.append(hangup)

    for sig in wanted:
        try:
            signal.signal(sig, _release_and_exit)
        except (OSError, ValueError):
            # Not the main thread, or a platform that will not take it. The
            # atexit path still covers the ordinary exit.
            pass


def _parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="stlink-mcp",
        description=(
            "An MCP server for STM32 targets over ST-LINK. It speaks MCP on stdin "
            "and stdout and is meant to be started by an MCP client; README.md "
            "has the client configuration."
        ),
        epilog=(
            "Settings are environment variables: STLINK_MCP_CLT_ROOT, "
            "STLINK_MCP_BOARDS, STLINK_MCP_CONSOLE, STLINK_MCP_CONSOLE_BAUD, "
            "STLINK_MCP_GDB_PORT and STLINK_MCP_NO_VENDOR_SEED."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--check", action="store_true",
        help=(
            "report the settings, toolchain, vendor database, board file and "
            "attached probes this server would use, then exit"
        ),
    )
    return parser.parse_args(argv)


def _check() -> int:
    """`stlink-mcp --check`: what the server would use, without starting it.

    Exit status 0 when it would start with a toolchain, 1 when no usable
    STM32CubeCLT or probe listing was found, 2 for a setting or board file the
    server would refuse. It lists probes with the programmer's `-l`, which
    neither halts nor resets anything and works while other probes are held.
    """
    print(f"stlink-mcp {__version__}")
    try:
        settings.check()
    except settings.SettingError as exc:
        print(f"settings: {exc}")
        return 2
    print("settings: usable")

    try:
        toolchain = server_mod._toolchain()
    except ToolchainError as exc:
        print(f"toolchain: {exc}")
        return 1
    print(f"toolchain: {toolchain.root}")
    if getattr(toolchain, "passed_over", ()):
        passed = ", ".join(str(p) for p in toolchain.passed_over)
        print(f"  passed over as incomplete, though newer: {passed}")
    print(f"vendor database: {server_mod.load_vendor_seed()}")

    try:
        count, path = server_mod.load_board_config()
    except BoardConfigError as exc:
        print(f"board file: {exc}")
        return 2
    print(f"board file: {path}, {count} board(s)" if path else "board file: none")

    listing = server_mod.list_probes()
    if listing["status"] != "verified":
        print(f"probes: {listing.get('error') or listing.get('unverified_because')}")
        for line in listing.get("errors", []):
            print(f"  {line}")
        return 1
    print(f"probes: {listing['count']} attached")
    for probe in listing["probes"]:
        console = probe["console_port"] or "no console mapped"
        if probe["console_port"]:
            console += f" ({probe['console_port_source']})"
        print(
            f"  {probe['serial']}  {probe['board'] or 'unknown board'}  "
            f"firmware {probe['firmware'] or 'unknown'}  console {console}"
        )
        if probe.get("console_note"):
            print(f"    {probe['console_note']}")
    if listing.get("unreadable_note"):
        print(f"  {listing['unreadable_note']}")
    return 0


def main(argv: list[str] | None = None) -> None:
    arguments = _parse_arguments(argv)
    if arguments.check:
        raise SystemExit(_check())

    _install_signal_handlers()

    # Settings first, and the same way a bad board file is handled: one line on
    # stderr and exit 2. Two of them used to be parsed at import time, so a typo
    # in the GDB port killed the process with a traceback before this ran.
    try:
        settings.check()
    except settings.SettingError as exc:
        print(f"stlink-mcp: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    # Board config is loaded here, before the server accepts anything, and a bad
    # file stops the process. That is deliberate. A user who wrote a board file
    # is telling this server what is on their desk; starting anyway with those
    # boards missing would answer every later question about a different machine
    # than the one they described, and the client would see a working server.
    #
    # stderr rather than stdout: stdout is the MCP transport.
    # Printed unconditionally. The previous version gated this on a non-zero
    # count, which is how a database format change that silently lost all 99
    # device IDs became indistinguishable from a machine with no toolchain.
    print(f"stlink-mcp: {server_mod.load_vendor_seed()}", file=sys.stderr)

    try:
        count, path = server_mod.load_board_config()
    except BoardConfigError as exc:
        print(f"stlink-mcp: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if count:
        print(f"stlink-mcp: loaded {count} user-described board(s) from {path}",
              file=sys.stderr)

    server_mod.mcp.run()


if __name__ == "__main__":
    main()
