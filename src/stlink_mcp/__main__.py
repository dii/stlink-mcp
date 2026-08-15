"""Entry point. Runs the MCP server on stdio, which is how MCP clients spawn it."""

from __future__ import annotations

import signal

from .runner import release_all_servers
from .server import mcp


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
    mcp.run()


if __name__ == "__main__":
    main()
