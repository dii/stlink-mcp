"""Entry point. Runs the MCP server on stdio, which is how MCP clients spawn it."""

from __future__ import annotations

import signal
import sys

from . import settings
from .quirks import BoardConfigError
from .runner import release_all_servers
from .server import load_board_config, load_vendor_seed, mcp


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


def main() -> None:
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
    print(f"stlink-mcp: {load_vendor_seed()}", file=sys.stderr)

    try:
        count, path = load_board_config()
    except BoardConfigError as exc:
        print(f"stlink-mcp: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if count:
        print(f"stlink-mcp: loaded {count} user-described board(s) from {path}",
              file=sys.stderr)

    mcp.run()


if __name__ == "__main__":
    main()
