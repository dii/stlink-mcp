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

With more than one board attached, every tool takes a `probe_serial`. Leaving it
out is only safe with a single probe, and the tools say so rather than quietly
picking one, because "the first probe" is not stable across replugs and loading
firmware onto the wrong board is a failure that looks like success.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp.server import MCPServer

from . import console as console_mod
from .probe import (
    Probe,
    ProbeInfo,
    TargetInfo,
    explain_connect_failure,
    list_probes as _list_probes,
)
from .runner import (
    RunError,
    acquire_server,
    held_serials,
    load_and_run,
    release_server,
    server_is_held,
)
from .toolchain import Toolchain, ToolchainError, find_toolchain

mcp = MCPServer("stlink-mcp")

_GDB_PORT_BASE = int(os.environ.get("STLINK_MCP_GDB_PORT", "61234"))
_CONSOLE_BAUD = int(os.environ.get("STLINK_MCP_CONSOLE_BAUD", "115200"))

# Cached per probe while its GDB server is held. Re-identifying would mean
# running STM32_Programmer_CLI, which cannot reach a probe the server has.
_held: dict[str | None, tuple[Probe, TargetInfo, ProbeInfo | None]] = {}

_HELD_MESSAGE = (
    "A GDB server is holding the probe, so this tool cannot reach it. That is "
    "deliberate: on some parts the debug access port cannot be re-acquired once "
    "firmware has run, so the server is kept alive to allow repeated runs. Call "
    "release_probe() to free it, but note that on those parts the board then "
    "needs a power cycle before firmware can be run again."
)


def _toolchain() -> Toolchain:
    toolchain = find_toolchain()
    toolchain.check()
    return toolchain


def _resolve_probe(
    toolchain: Toolchain, probe_serial: str | None
) -> tuple[str | None, ProbeInfo | None, str | None]:
    """Pick which probe to talk to. Returns (serial, info, error)."""
    if server_is_held():
        # Enumeration needs the programmer CLI, which a held server blocks. Fall
        # back to what was cached when the server was acquired.
        if probe_serial in _held:
            return probe_serial, _held[probe_serial][2], None
        if probe_serial is None and len(_held) == 1:
            only = next(iter(_held))
            return only, _held[only][2], None

    attached = _list_probes(toolchain)
    if not attached:
        return None, None, "No ST-LINK probes found."

    if probe_serial is None:
        if len(attached) > 1:
            listing = ", ".join(p.serial for p in attached)
            return None, None, (
                f"{len(attached)} probes are attached and no probe_serial was given. "
                f"Pass one of: {listing}. Not choosing for you, because probe order "
                "is not stable across replugs and using the wrong board is a "
                "failure that looks like success."
            )
        return attached[0].serial, attached[0], None

    for info in attached:
        if info.serial == probe_serial:
            return info.serial, info, None

    listing = ", ".join(p.serial for p in attached)
    return None, None, f"No probe with serial {probe_serial}. Attached: {listing}"


def _gdb_port_for(serial: str | None, toolchain: Toolchain) -> int:
    """A stable, distinct GDB port per probe, so boards do not collide."""
    if serial is None:
        return _GDB_PORT_BASE
    attached = [p.serial for p in _list_probes(toolchain)] if not server_is_held() else []
    if serial in attached:
        return _GDB_PORT_BASE + attached.index(serial)
    return _GDB_PORT_BASE + (hash(serial) % 100)


@mcp.tool()
def list_probes() -> dict[str, Any]:
    """List every attached ST-LINK, with its firmware and its serial console.

    The serial-to-console mapping is the useful part with more than one board:
    it is not positional, so pairing probes with /dev/ttyACM* by order gets it
    wrong.
    """
    try:
        toolchain = _toolchain()
    except ToolchainError as exc:
        return {"ok": False, "error": str(exc)}

    if server_is_held():
        return {
            "ok": False,
            "error": _HELD_MESSAGE,
            "held_serials": [s for s in held_serials()],
        }

    probes = _list_probes(toolchain)
    return {
        "ok": True,
        "count": len(probes),
        "probes": [
            {
                "serial": p.serial,
                "firmware": p.firmware,
                "board": p.board,
                "console_port": p.console_port,
            }
            for p in probes
        ],
    }


@mcp.tool()
def board_state(probe_serial: str | None = None) -> dict[str, Any]:
    """Identify a target and report what is known about it.

    Tries each debug access port, because a wrong access port and an absent
    target produce the same error message from the vendor tools.
    """
    try:
        toolchain = _toolchain()
    except ToolchainError as exc:
        return {"ok": False, "error": str(exc)}

    serial, probe_info, error = _resolve_probe(toolchain, probe_serial)
    if error:
        return {"ok": False, "error": error}

    if serial in _held:
        probe, info, probe_info = _held[serial]
    else:
        probe = Probe(toolchain, serial=serial)
        info = probe.identify()

    quirks = info.quirks
    state: dict[str, Any] = {
        "ok": info.connected,
        "probe_serial": serial,
        "stlink": {"serial": info.stlink_serial, "firmware": info.stlink_firmware},
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
        "console_port": probe_info.console_port if probe_info else None,
        "gdb_server_held": server_is_held(serial),
    }

    if not info.connected:
        state["errors"] = info.errors
        state["hint"] = explain_connect_failure(info)
    if probe.degraded_reason:
        state["ok"] = False
        state["warning"] = probe.degraded_reason

    return state


@mcp.tool()
def run_firmware(
    elf_path: str,
    probe_serial: str | None = None,
    console_seconds: float = 3.0,
) -> dict[str, Any]:
    """Load an ELF into target RAM, start it, and verify that it started.

    Verification is console output from THAT board's own serial port, captured
    from before the target is resumed so start-up banners are not missed. If the
    target produces nothing, this reports ok=false even though every underlying
    command succeeded, because a silent target is the symptom this project
    exists to catch.

    Does not sign anything: an image placed directly in RAM by a debugger
    bypasses the boot ROM.
    """
    try:
        toolchain = _toolchain()
    except ToolchainError as exc:
        return {"ok": False, "error": str(exc)}

    serial, probe_info, error = _resolve_probe(toolchain, probe_serial)
    if error:
        return {"ok": False, "error": error}

    if serial in _held:
        probe, info, probe_info = _held[serial]
    else:
        probe = Probe(toolchain, serial=serial)
        info = probe.identify()
        if not info.connected:
            return {
                "ok": False,
                "error": "No target",
                "probe_serial": serial,
                "errors": info.errors,
                "hint": explain_connect_failure(info),
            }
        if probe.degraded_reason:
            return {"ok": False, "probe_serial": serial, "error": probe.degraded_reason}

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
    # This board's own console, not whichever ttyACM* happens to be first.
    port = probe_info.console_port if probe_info else console_mod.find_console_port()
    gdb_port = _gdb_port_for(serial, toolchain)

    try:
        acquire_server(toolchain, probe.access_port, gdb_port, serial=serial)
        _held[serial] = (probe, info, probe_info)

        if port:
            with console_mod.ConsoleCapture(port, _CONSOLE_BAUD) as cap:
                sp, pc, size, _ = load_and_run(
                    toolchain, elf, quirks.vector_table_addr, probe.access_port, gdb_port
                )
                text = cap.collect(console_seconds)
        else:
            sp, pc, size, _ = load_and_run(
                toolchain, elf, quirks.vector_table_addr, probe.access_port, gdb_port
            )
            text = ""
    except (RunError, ToolchainError, console_mod.ConsoleError) as exc:
        return {"ok": False, "probe_serial": serial, "error": str(exc)}

    started = bool(text.strip())
    result: dict[str, Any] = {
        "ok": started if port else True,
        "probe_serial": serial,
        "console_port": port,
        "stack_pointer": _hex(sp),
        "program_counter": _hex(pc),
        "load_size_bytes": size,
        "console": text,
        "verified_by": "console output" if port else "nothing",
    }

    if quirks.debug_closes_on_run:
        result["next_run_note"] = (
            f"On {quirks.name}, firmware closes the debug access port as it starts, "
            "and no reset reopens it. The GDB server is therefore being HELD so "
            "further run_firmware calls on this board keep working without a power "
            "cycle. read_memory, core_registers and flash_firmware cannot reach the "
            "probe until release_probe() is called, and releasing it means a power "
            "cycle before firmware can run again."
        )

    if not port:
        result["warning"] = (
            "No serial console for this probe, so this reports that the commands "
            "succeeded, NOT that the firmware is running."
        )
    elif not started:
        result["error"] = (
            "The load and start commands succeeded but the target produced no "
            "console output. The firmware is probably not running, or it does not "
            "print. core_registers() shows where the program counter actually is, "
            "after release_probe()."
        )

    return result


@mcp.tool()
def release_probe(probe_serial: str | None = None) -> dict[str, Any]:
    """Shut down held GDB servers and free the probes.

    With no serial, releases all of them. Needed before read_memory,
    core_registers or flash_firmware can be used after a run. On parts where the
    access port cannot be re-acquired once firmware has run, this is a one-way
    door until the board is power cycled, so it is never done implicitly.
    """
    affected = [probe_serial] if probe_serial is not None else list(_held)
    quirks = [_held[s][1].quirks for s in affected if s in _held]

    released = release_server(probe_serial)
    for s in affected:
        _held.pop(s, None)

    result: dict[str, Any] = {"ok": True, "released": released, "probes": affected}
    if released and any(q.debug_closes_on_run for q in quirks):
        result["warning"] = (
            "The debug access port cannot be re-acquired now that firmware has "
            "run. Power cycle the affected board(s) before the next run_firmware."
        )
    return result


@mcp.tool()
def flash_firmware(
    image_path: str, probe_serial: str | None = None, address: int | None = None
) -> dict[str, Any]:
    """Program a signed image into a target's boot memory for standalone boot.

    Prefer run_firmware for edit-build-run: it is faster and needs no signing.
    This tool does not sign, because signing needs keys and options that belong
    to the project rather than to a programmer.
    """
    if server_is_held():
        return {"ok": False, "error": _HELD_MESSAGE, "held_serials": held_serials()}
    try:
        toolchain = _toolchain()
    except ToolchainError as exc:
        return {"ok": False, "error": str(exc)}

    serial, _, error = _resolve_probe(toolchain, probe_serial)
    if error:
        return {"ok": False, "error": error}

    probe = Probe(toolchain, serial=serial)
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
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "probe_serial": serial,
        "address": _hex(target_address),
        "signing_required": quirks.requires_signing,
        "detail": output.strip().splitlines()[-3:],
        "next_step": (
            "This does not start the image. Set the board's boot configuration to "
            "boot from flash and reset it."
        ),
    }


@mcp.tool()
def read_memory(
    address: int, length_bytes: int = 32, probe_serial: str | None = None
) -> dict[str, Any]:
    """Read target memory as 32-bit words. Does not halt the core."""
    if server_is_held():
        return {"ok": False, "error": _HELD_MESSAGE, "held_serials": held_serials()}
    try:
        toolchain = _toolchain()
        serial, _, error = _resolve_probe(toolchain, probe_serial)
        if error:
            return {"ok": False, "error": error}
        probe = Probe(toolchain, serial=serial)
        probe.identify()
        words = probe.read_memory(address, length_bytes)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "probe_serial": serial,
        "words": {f"0x{a:08X}": f"0x{v:08X}" for a, v in sorted(words.items())},
    }


@mcp.tool()
def core_registers(probe_serial: str | None = None) -> dict[str, Any]:
    """Read the core registers. This HALTS the core.

    The most direct way to find out what a program is really doing, and what
    settles "did it actually start" when there is no console. Perturbing, so it
    is never used inside the routine checks.
    """
    if server_is_held():
        return {"ok": False, "error": _HELD_MESSAGE, "held_serials": held_serials()}
    try:
        toolchain = _toolchain()
        serial, _, error = _resolve_probe(toolchain, probe_serial)
        if error:
            return {"ok": False, "error": error}
        probe = Probe(toolchain, serial=serial)
        probe.identify()
        regs = probe.core_registers()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "probe_serial": serial,
        "halted": True,
        "registers": {name: f"0x{value:08X}" for name, value in regs.items()},
        "note": "The core is now halted. Resume it by running firmware again.",
    }


@mcp.tool()
def capture_console(
    seconds: float = 5.0, probe_serial: str | None = None
) -> dict[str, Any]:
    """Capture what a target prints on its serial console.

    Resolves the port from the probe's serial rather than taking the first
    /dev/ttyACM*, which with two boards attached is the wrong one about half the
    time.
    """
    port: str | None = None
    try:
        toolchain = _toolchain()
        _, probe_info, error = _resolve_probe(toolchain, probe_serial)
        if not error and probe_info is not None:
            port = probe_info.console_port
    except ToolchainError:
        pass

    if port is None:
        port = console_mod.find_console_port()
    if port is None:
        return {"ok": False, "error": "No serial console found."}

    try:
        text = console_mod.capture(port, seconds, _CONSOLE_BAUD)
    except console_mod.ConsoleError as exc:
        return {"ok": False, "error": str(exc)}

    return {"ok": True, "port": port, "seconds": seconds, "output": text}


def _hex(value: int | None) -> str | None:
    return None if value is None else f"0x{value:08X}"
