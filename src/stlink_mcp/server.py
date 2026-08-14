"""The MCP tool surface.

Design rule for everything in this file: a tool reports success only after
checking that the thing it was asked to do actually happened. The underlying
vendor tools do not do this. STM32_Programmer_CLI will print "Start operation
achieved successfully" for a start that left the program counter untouched, and
a GDB load against an already-running target completes cleanly while changing
nothing. An agent driving those CLIs directly inherits both lies.

So: "ran" means the target produced output, not that a command exited zero.
Where a claim cannot be verified, the result says so rather than implying it
was checked.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp.server import MCPServer

from . import console as console_mod
from .probe import Probe, explain_connect_failure
from .runner import GdbServer, RunError, load_and_run
from .toolchain import ToolchainError, find_toolchain

mcp = MCPServer("stlink-mcp")

_GDB_PORT = int(os.environ.get("STLINK_MCP_GDB_PORT", "61234"))
_CONSOLE_BAUD = int(os.environ.get("STLINK_MCP_CONSOLE_BAUD", "115200"))


def _probe() -> Probe:
    toolchain = find_toolchain()
    toolchain.check()
    return Probe(toolchain)


@mcp.tool()
def board_state() -> dict[str, Any]:
    """Identify the attached target and report what is known about it.

    Tries each debug access port, because a wrong access port and an absent
    target produce the same error message from the vendor tools.
    """
    try:
        probe = _probe()
    except ToolchainError as exc:
        return {"ok": False, "error": str(exc)}

    info = probe.identify()
    quirks = info.quirks
    port = console_mod.find_console_port()

    state: dict[str, Any] = {
        "ok": info.connected,
        "stlink": {
            "serial": info.stlink_serial,
            "firmware": info.stlink_firmware,
        },
        "board": info.board,
        "device": {
            "id": f"0x{info.device_id:X}" if info.device_id is not None else None,
            "name": info.device_name,
            "cpu": info.cpu,
            "revision": info.revision,
        },
        "access_port": probe.access_port,
        "family": {
            "name": quirks.name,
            "requires_signing_for_flash": quirks.requires_signing,
            "ram_load_base": _hex(quirks.ram_load_base),
            "vector_table": _hex(quirks.vector_table_addr),
            "flash_base": _hex(quirks.flash_base),
            "external_loader": quirks.external_loader,
            "notes": quirks.notes,
        },
        "console_port": port,
    }

    if not info.connected:
        state["errors"] = info.errors
        state["hint"] = explain_connect_failure(info)

    if probe.degraded_reason:
        state["ok"] = False
        state["warning"] = probe.degraded_reason

    return state


@mcp.tool()
def run_firmware(elf_path: str, console_seconds: float = 3.0) -> dict[str, Any]:
    """Load an ELF into target RAM, start it, and verify that it started.

    Verification is console output, captured from before the target is resumed
    so that start-up banners are not missed. If the target produces nothing, this
    reports ok=false even though every underlying command succeeded, because a
    silent target is the symptom this whole project exists to catch.

    Needs a family with a known RAM load address. It does not sign anything: an
    image placed directly in RAM by a debugger bypasses the boot ROM.
    """
    try:
        probe = _probe()
    except ToolchainError as exc:
        return {"ok": False, "error": str(exc)}

    info = probe.identify()
    if not info.connected:
        return {
            "ok": False,
            "error": "No target",
            "errors": info.errors,
            "hint": explain_connect_failure(info),
        }

    quirks = info.quirks
    if quirks.vector_table_addr is None:
        return {
            "ok": False,
            "error": (
                f"No RAM-execution layout is recorded for {quirks.name}. "
                "Add it to quirks.py once verified on hardware, rather than guessing."
            ),
        }

    elf = Path(elf_path).expanduser()
    port = console_mod.find_console_port()

    try:
        if port:
            with console_mod.ConsoleCapture(port, _CONSOLE_BAUD) as cap:
                with GdbServer(probe.toolchain, probe.access_port, _GDB_PORT):
                    sp, pc, size, output = load_and_run(
                        probe.toolchain, elf, quirks.vector_table_addr,
                        probe.access_port, _GDB_PORT,
                    )
                text = cap.collect(console_seconds)
        else:
            with GdbServer(probe.toolchain, probe.access_port, _GDB_PORT):
                sp, pc, size, output = load_and_run(
                    probe.toolchain, elf, quirks.vector_table_addr,
                    probe.access_port, _GDB_PORT,
                )
            text = ""
    except (RunError, ToolchainError, console_mod.ConsoleError) as exc:
        return {"ok": False, "error": str(exc)}

    started = bool(text.strip())
    result: dict[str, Any] = {
        "ok": started if port else True,
        "stack_pointer": _hex(sp),
        "program_counter": _hex(pc),
        "load_size_bytes": size,
        "console": text,
        "verified_by": "console output" if port else "nothing",
    }

    if quirks.debug_closes_on_run:
        result["next_run_note"] = (
            f"On {quirks.name}, running firmware closes the debug access port and "
            "a reset does not reopen it. Power cycle the board (unplug and replug "
            "USB) before the next run, or the next call will report the access "
            "port as unreachable."
        )

    if not port:
        result["warning"] = (
            "No serial console was found, so this reports that the commands "
            "succeeded, NOT that the firmware is running. Set STLINK_MCP_CONSOLE "
            "to verify properly."
        )
    elif not started:
        result["error"] = (
            "The load and start commands succeeded but the target produced no "
            "console output. The firmware is probably not running, or it does "
            "not print. Check with core_registers(), which shows where the "
            "program counter actually is."
        )

    return result


@mcp.tool()
def flash_firmware(image_path: str, address: int | None = None) -> dict[str, Any]:
    """Program a signed image into the target's boot memory for standalone boot.

    This is the standalone path, not the development one. Prefer run_firmware
    for edit-build-run: it is faster and needs no signing.

    On families whose boot ROM authenticates the image, the file must already be
    signed. This tool does not sign it, because signing needs keys and options
    that belong to the project rather than to the programmer.
    """
    try:
        probe = _probe()
    except ToolchainError as exc:
        return {"ok": False, "error": str(exc)}

    info = probe.identify()
    if not info.connected:
        return {"ok": False, "error": "No target", "hint": explain_connect_failure(info)}

    quirks = info.quirks
    target_address = address if address is not None else quirks.flash_base
    if target_address is None:
        return {
            "ok": False,
            "error": f"No flash base recorded for {quirks.name}; pass address explicitly.",
        }

    try:
        output = probe.program_flash(
            str(Path(image_path).expanduser()), target_address, quirks.external_loader
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller verbatim
        return {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "address": _hex(target_address),
        "external_loader": quirks.external_loader,
        "signing_required": quirks.requires_signing,
        "detail": output.strip().splitlines()[-3:],
        "next_step": (
            "This does not start the image. Set the board's boot configuration "
            "to boot from flash and reset it."
        ),
    }


@mcp.tool()
def read_memory(address: int, length_bytes: int = 32) -> dict[str, Any]:
    """Read target memory as 32-bit words. Does not halt the core."""
    try:
        probe = _probe()
        probe.identify()
        words = probe.read_memory(address, length_bytes)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "words": {f"0x{addr:08X}": f"0x{value:08X}" for addr, value in sorted(words.items())},
    }


@mcp.tool()
def core_registers() -> dict[str, Any]:
    """Read the core registers. This HALTS the core.

    The most direct way to find out what a program is really doing, and the tool
    that settles "did it actually start" when there is no console. Perturbing, so
    it is never used inside the routine checks.
    """
    try:
        probe = _probe()
        probe.identify()
        regs = probe.core_registers()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "halted": True,
        "registers": {name: f"0x{value:08X}" for name, value in regs.items()},
        "note": "The core is now halted. Resume it by running firmware again.",
    }


@mcp.tool()
def capture_console(seconds: float = 5.0) -> dict[str, Any]:
    """Capture whatever the target prints on its serial console."""
    port = console_mod.find_console_port()
    if port is None:
        return {
            "ok": False,
            "error": "No serial console found. Set STLINK_MCP_CONSOLE to the device path.",
        }
    try:
        text = console_mod.capture(port, seconds, _CONSOLE_BAUD)
    except console_mod.ConsoleError as exc:
        return {"ok": False, "error": str(exc)}

    return {"ok": True, "port": port, "seconds": seconds, "output": text}


def _hex(value: int | None) -> str | None:
    return None if value is None else f"0x{value:08X}"
