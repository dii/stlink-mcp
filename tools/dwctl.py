#!/usr/bin/env python3
"""A long-lived controller that holds SWD sessions open.

Why this exists, and why it is the shape the MCP server already has:

On the STM32N6 the debug access port cannot be re-acquired once firmware has
run. An *already established* session keeps working across repeated cycles, so
the cost of that quirk is paid once per session rather than once per iteration,
provided something stays alive holding the session. A one-shot script does not:
its atexit handler releases the session and the next invocation is locked out.

Holding the session is necessary and not sufficient. Every load must also be
preceded by an explicit reset, or each image after the first inherits the
previous run's live interrupt state and faults during start-up. That is not a
detail of this harness; it is why this harness exists, since finding it needed
many consecutive runs through one session.

That is exactly the difference between driving these tools from a script and
driving them from a long-running MCP server, and it is why this file is a
development harness rather than part of the library.

Protocol, deliberately trivial so it can be driven from a shell:

    echo 'run <serial> <elf>'        > /tmp/dwctl.fifo
    echo 'cap <serial> <seconds>'    > /tmp/dwctl.fifo
    echo 'status'                    > /tmp/dwctl.fifo
    echo 'quit'                      > /tmp/dwctl.fifo

Results are appended to /tmp/dwctl.log.
"""

from __future__ import annotations

import os
import sys
import threading
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from stlink_mcp import console as cm  # noqa: E402
from stlink_mcp.server import list_probes, run_firmware  # noqa: E402

FIFO = "/tmp/dwctl.fifo"
LOG = "/tmp/dwctl.log"

_probes: dict[str, dict] = {}


def say(text: str) -> None:
    with open(LOG, "a") as handle:
        handle.write(text.rstrip() + "\n")
        handle.flush()


def cmd_status() -> None:
    for serial, info in _probes.items():
        say(f"  {serial[:8]}  {info['console_port']}")


def cmd_run(serial: str, elf: str) -> None:
    result = run_firmware(elf, probe_serial=serial, console_seconds=1.0)
    name = os.path.basename(elf)
    say(f"  run {name} on {serial[:8]}: ok={result['ok']}"
        + ("" if result["ok"] else f"  err={str(result.get('error'))[:120]}"))


def cmd_cap(serials: list[str], seconds: float) -> None:
    """Capture several consoles at once, so a transmitter and its receivers are
    observed over the same window rather than consecutive ones."""
    out: dict[str, str] = {}

    def grab(serial: str) -> None:
        port = _probes[serial]["console_port"]
        try:
            with cm.ConsoleCapture(port, 115200) as cap:
                out[serial] = cap.collect(seconds)
        except Exception as exc:  # noqa: BLE001
            out[serial] = f"<{exc}>"

    threads = [threading.Thread(target=grab, args=(s,)) for s in serials]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    for serial in serials:
        text = out.get(serial, "")
        tx = sum(1 for line in text.splitlines() if line.startswith("TX seq="))
        rx = sum(1 for line in text.splitlines() if line.startswith("RX seq="))
        tail = [line for line in text.splitlines() if line.startswith("...")][-1:]
        say(f"  {serial[:8]}  TX={tx:4d} RX={rx:4d}  {tail[0] if tail else ''}")
        with open(f"/tmp/dwctl_{serial[:8]}.log", "w") as handle:
            handle.write(text)


def main() -> int:
    global _probes

    if os.path.exists(FIFO):
        os.unlink(FIFO)
    os.mkfifo(FIFO)
    open(LOG, "w").close()

    listed = list_probes()
    if not listed.get("ok"):
        say(f"cannot list probes: {listed.get('error')}")
        return 1
    _probes = {p["serial"]: p for p in listed["probes"]}
    say(f"ready, {len(_probes)} probes, sessions will be HELD until quit")
    cmd_status()

    while True:
        with open(FIFO) as fifo:
            for line in fifo:
                parts = line.split()
                if not parts:
                    continue
                say(f"> {line.strip()}")
                try:
                    if parts[0] == "quit":
                        say("bye")
                        return 0
                    if parts[0] == "status":
                        cmd_status()
                    elif parts[0] == "run" and len(parts) >= 3:
                        cmd_run(parts[1], parts[2])
                    elif parts[0] == "cap" and len(parts) >= 3:
                        cmd_cap(parts[1].split(","), float(parts[2]))
                    else:
                        say(f"  ? {line.strip()}")
                except Exception:  # noqa: BLE001
                    say("  EXCEPTION\n" + traceback.format_exc())


if __name__ == "__main__":
    sys.exit(main())
