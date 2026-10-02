"""Check what the README promises against attached boards, through the server.

    uv run python tools/hwcheck/hwcheck.py [--h7 SERIAL] [--n6 SERIAL]
                                           [--jtag SERIAL] [--flash]
                                           [--f4 SERIAL [--f4-console PORT]]
                                           [--n6-closed]

Starts the server over stdio, as an MCP client does, and runs each check as
tool calls against a NUCLEO-H743ZI and a NUCLEO-N657X0-Q, whichever are
attached. Without serials it finds them by board name in list_probes. The test
images are built fresh from images/ with STM32CubeCLT's compiler.

What it changes on the boards: it loads RAM images and resets the H7, which
ends running its own firmware again. It never resets the N6, whose stock
start-up code can close its debug port until a power cycle; the N6 is left
running a RAM image, which a power cycle clears. --flash also writes the H7's
flash: it saves the whole flash first with the programmer, writes a test
image, and writes the saved copy back and verifies it, even when a check fails.
--jtag names an ST-LINK wired to the H7's JTAG connector, and adds checks over
JTAG through it. --n6-closed ends the N6's checks by closing its debug port
the way ST's start-up code does and checking what the tools say to it; the N6
then needs a power cycle. --f4 names the ST-LINK on an STM32F407 board, whose RAM
images are loaded and which is reset at the end; with --flash its flash is
saved, written and restored the same way.

Each check prints PASS or FAIL with what it saw; the exit status is 1 when any
check failed. docs/findings.md records what each check stands for.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, str(Path(__file__).parent))
from build_images import Built, build  # noqa: E402

from stlink_mcp.toolchain import find_toolchain  # noqa: E402

H7_BOARD, N6_BOARD = "NUCLEO-H743ZI", "NUCLEO-N657X0-Q"
H7_COUNTER, N6_COUNTER, F4_COUNTER = "0x24070000", "0x341B0000", "0x2001F000"


CALLS: list[tuple[str, dict, dict]] = []
"""Every call since the last check, printed whole when that check fails."""


class Server:
    """One stlink-mcp server over stdio."""

    def __init__(self, env: dict[str, str] | None = None) -> None:
        self.env = {**os.environ, "STLINK_MCP_NO_VENDOR_SEED": "1", **(env or {})}
        self._stack = AsyncExitStack()
        self.session: ClientSession | None = None

    async def __aenter__(self) -> "Server":
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "stlink_mcp"], env=self.env
        )
        # The server traces every call on stderr (N9); kept out of the report.
        self.errlog = self._stack.enter_context(tempfile.TemporaryFile("w+"))
        read, write = await self._stack.enter_async_context(stdio_client(params, errlog=self.errlog))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._stack.aclose()

    async def call(self, tool: str, **args: object) -> dict:
        assert self.session is not None
        result = await self.session.call_tool(tool, args)
        body = result.structured_content
        if body is None:
            body = json.loads(result.content[0].text)
        CALLS.append((tool, args, body))
        return body


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[bool, str, str]] = []

    def check(self, ok: bool, name: str, saw: str) -> bool:
        self.rows.append((ok, name, saw))
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"\n        {saw}" if saw else ""), flush=True)
        if not ok:
            for tool, args, body in CALLS:
                print(f"        {tool}({json.dumps(args)}) returned:", flush=True)
                for line in json.dumps(body, indent=2).splitlines():
                    print(f"          {line}", flush=True)
        CALLS.clear()
        return ok

    @property
    def failed(self) -> int:
        return sum(1 for ok, _, _ in self.rows if not ok)


def _why(result: dict) -> str:
    text = result.get("verified_by") or result.get("unverified_because") or result.get("error") or ""
    return f"{result.get('status')}: {text[:160]}"


def _answered(board: dict, label: str) -> bool:
    """A board that does not answer its first call would fail every check after
    it the slow way, up to a minute each, so its first FAIL stands for them."""
    if board["status"] == "verified":
        return True
    print(f"SKIP  {label}: its other checks, because the board did not answer", flush=True)
    return False


def _udf_address(elf: Path) -> int:
    objdump = find_toolchain().root / "GNU-tools-for-STM32" / "bin" / "arm-none-eabi-objdump"
    listing = subprocess.run([str(objdump), "-d", str(elf)], capture_output=True, text=True, check=True).stdout
    match = re.search(r"^\s*([0-9a-f]+):\s+de00\s+udf", listing, re.M)
    if not match:
        raise SystemExit(f"no udf in {elf}")
    return int(match.group(1), 16)


def _counter(result: dict, address: str) -> int | None:
    word = result.get("words", {}).get(address)
    return int(word, 16) if word else None


async def check_h7(server: Server, serial: str, images: Built, report: Report) -> bool:
    s = serial

    board = await server.call("board_state", probe_serial=s)
    report.check(
        board["status"] == "verified" and board["family"]["name"] == "STM32H7"
        and "access_port_warning" not in board,
        "H7: board_state identifies the family on a measured port", _why(board))
    if not _answered(board, "H7"):
        return False

    run = await server.call("run_firmware", elf_path=str(images.elf["h7_ram"]), probe_serial=s, console_seconds=1.5)
    report.check(run["status"] == "verified" and "ram tick 0" in run.get("console", ""),
                 "H7: run_firmware is verified by the new image's own console", _why(run))
    held = await server.call("read_memory", address=H7_COUNTER, probe_serial=s)
    report.check(held["status"] == "failed" and "holding this probe" in held.get("error", "").lower(),
                 "H7: a held probe refuses read_memory and says why", _why(held))
    cached = await server.call("board_state", probe_serial=s)
    report.check(cached["status"] == "unverified", "H7: board_state answers from cache while held", _why(cached))
    await server.call("release_probe", probe_serial=s)

    first = await server.call("read_memory", address=H7_COUNTER, length_bytes=4, probe_serial=s)
    await anyio.sleep(1.0)
    second = await server.call("read_memory", address=H7_COUNTER, length_bytes=4, probe_serial=s)
    a, b = _counter(first, H7_COUNTER), _counter(second, H7_COUNTER)
    report.check(a is not None and b is not None and b > a,
                 "H7: the image keeps running after release (its counter advances)", f"{a and hex(a)} then {b and hex(b)}")

    await anyio.sleep(3.0)  # the running image prints while nothing has the port open
    silent = await server.call("run_firmware", elf_path=str(images.elf["h7_silent"]), probe_serial=s, console_seconds=2)
    report.check(silent["status"] == "unverified" and silent.get("console") == "" and silent.get("console_buffered"),
                 "H7: a silent image after a printing one is unverified; the backlog is set apart",
                 f"{_why(silent)}; set apart {silent.get('console_buffered_bytes')} bytes")
    await server.call("release_probe", probe_serial=s)

    for name, wanted in (("h7_flashlinked", "not linked for this family's RAM"),
                         ("h7_mixed", "outside the SRAM region")):
        refused = await server.call("run_firmware", elf_path=str(images.elf[name]), probe_serial=s)
        report.check(refused["status"] == "failed" and wanted in refused.get("error", ""),
                     f"H7: run_firmware refuses {name} before loading", _why(refused))
    hole = await server.call("run_firmware", elf_path=str(images.elf["h7_hole"]), probe_serial=s, console_seconds=1)
    report.check(hole["status"] == "failed" and any("0x2c000000" in line for line in hole.get("gdb_output", [])),
                 "H7: a load that fails partway is failed, with GDB's own output", _why(hole))
    await server.call("release_probe", probe_serial=s)
    elf_flash = await server.call("flash_firmware", image_path=str(images.elf["h7_flash"]), probe_serial=s)
    report.check(elf_flash["status"] == "failed" and "not a raw binary" in elf_flash.get("error", ""),
                 "H7: flash_firmware refuses an ELF", _why(elf_flash))
    odd = await server.call("read_memory", address="0x24070001", probe_serial=s)
    report.check(odd["status"] == "failed", "H7: read_memory refuses a misaligned address", _why(odd))

    for name, stack, fp in (("h7_fault", "MSP", False), ("h7_fault_psp", "PSP", False), ("h7_fault_fp", "MSP", True)):
        elf = images.elf[name]
        await server.call("run_firmware", elf_path=str(elf), probe_serial=s, console_seconds=3)
        await server.call("release_probe", probe_serial=s)
        await anyio.sleep(1.0)
        where = await server.call("locate_fault", probe_serial=s, elf_path=str(elf))
        report.check(
            where["status"] == "verified" and where.get("stack") == stack and where.get("fp_frame") is fp
            and where.get("faulting_pc") == f"0x{_udf_address(elf):08X}",
            f"H7: locate_fault finds the udf of {name} on {stack}{' with an FP frame' if fp else ''}",
            f"{_why(where)}; pc {where.get('faulting_pc')}, function {where.get('faulting_function', {}).get('function')}")
    status = await server.call("read_fault_status", probe_serial=s)
    report.check(any(f["bit"] == "UNDEFINSTR" for f in status.get("faults", [])),
                 "H7: read_fault_status decodes the undefined instruction", _why(status))

    await server.call("run_firmware", elf_path=str(images.elf["h7_fault_lr"]), probe_serial=s, console_seconds=3)
    await server.call("release_probe", probe_serial=s)
    await anyio.sleep(1.0)
    lr = await server.call("locate_fault", probe_serial=s)
    report.check(lr["status"] == "failed" and "not an EXC_RETURN" in lr.get("error", ""),
                 "H7: locate_fault refuses when the handler has overwritten LR", _why(lr))

    await server.call("run_firmware", elf_path=str(images.elf["h7_busfault"]), probe_serial=s, console_seconds=3)
    await server.call("release_probe", probe_serial=s)
    await anyio.sleep(1.0)
    bus = await server.call("read_fault_status", probe_serial=s)
    late = await server.call("locate_fault", probe_serial=s, elf_path=str(images.elf["h7_busfault"]))
    report.check(any(f["bit"] == "IMPRECISERR" for f in bus.get("faults", []))
                 and any("imprecise" in n for n in late.get("notes", [])),
                 "H7: an imprecise bus error is decoded, and locate_fault says its PC is late",
                 f"{_why(bus)}; locate {late.get('faulting_pc')}")

    regs = await server.call("core_registers", probe_serial=s)
    report.check(regs["status"] == "verified" and regs.get("halted_to_read_registers") is True and "halted" not in regs,
                 "H7: core_registers halts for the read and claims nothing after", _why(regs))

    await server.call("run_firmware", elf_path=str(images.elf["h7_ram"]), probe_serial=s, console_seconds=1)
    await server.call("release_probe", probe_serial=s)
    capture = await server.call("capture_console", probe_serial=s, seconds=2)
    report.check(capture["status"] == "verified" and "tick" in capture.get("output", ""),
                 "H7: capture_console reads the probe's own port", f"{_why(capture)}; port {capture.get('port')}")

    await server.call("run_firmware", elf_path=str(images.elf["h7_itm"]), probe_serial=s, console_seconds=1)
    await server.call("release_probe", probe_serial=s)
    swo = await server.call("capture_swo", probe_serial=s, core_clock_hz=64_000_000, seconds=2)
    report.check(swo["status"] == "verified" and "itm tick" in swo.get("output", "") and swo.get("trace_left_enabled"),
                 "H7: capture_swo reads ITM port 0 over SWO at the core's 64 MHz, and says trace stays on", _why(swo))

    await check_names(server, "H7", s, images, report, "h7_vars", "f4_vars", "0x58024400", "board name")

    reset = await server.call("reset_target", probe_serial=s)
    report.check(reset["status"] == "verified", "H7: reset_target halts at the reset handler the vector table names", _why(reset))
    idle = await server.call("release_probe", probe_serial=s)
    report.check(idle["status"] == "unverified", "H7: release_probe with nothing held is unverified", _why(idle))
    return True


async def check_names(server: Server, label: str, serial: str, images: Built, report: Report,
                      own: str, other: str, register_address: str, svd_says: str) -> None:
    """Read a variable and a register by name, on a board running images[own]."""
    s = serial
    await server.call("run_firmware", elf_path=str(images.elf[own]), probe_serial=s, console_seconds=1)
    await server.call("release_probe", probe_serial=s)
    var = await server.call("read_memory", probe_serial=s, symbol="g_state", elf_path=str(images.elf[own]))
    value = var.get("value") or {}
    report.check(var["status"] == "verified" and value.get("temperature_mc") == -1234 and value.get("gain") == 1.5
                 and value.get("mode") == "MODE_RUN (3)" and value.get("flags") == {"a": 5, "b": 17, "c": 0xBEEF}
                 and value.get("pos") == {"x": -3, "y": 7},
                 f"{label}: read_memory reads a struct by name, decoded with the ELF's types", _why(var))
    wrong = await server.call("read_memory", probe_serial=s, symbol="g_state", elf_path=str(images.elf[other]))
    # Unverified where the other part's addresses hold memory here (the F407's on
    # the H7), failed where they do not (the H7's on the F407).
    why = wrong.get("unverified_because") or wrong.get("error") or ""
    report.check(wrong["status"] != "verified" and "value" not in wrong
                 and ("not running this ELF" in why or "another part" in why),
                 f"{label}: a read by name with an ELF the target does not run is not verified", _why(wrong))
    reg = await server.call("read_memory", probe_serial=s, register="RCC.CR")
    raw = await server.call("read_memory", probe_serial=s, address=register_address, length_bytes=4)
    report.check(reg["status"] == "verified" and svd_says in reg.get("svd_source", "")
                 and reg.get("value") == raw.get("words", {}).get(register_address),
                 f"{label}: read_memory reads RCC.CR by name and agrees with the raw word",
                 f"{_why(reg)}; {reg.get('value')} against {raw.get('words', {}).get(register_address)}; {reg.get('svd_source')}")


async def check_flash(server: Server, label: str, serial: str, flash_size: int,
                      image: Path, report: Report) -> None:
    """Write a board's flash, read it back, and restore the board's own image."""
    programmer = str(find_toolchain().programmer)
    connect = ["-c", "port=SWD", f"sn={serial}", "mode=UR"]
    backup = Path(tempfile.mkdtemp(prefix="stlink-hwcheck-flash-")) / f"{label.lower()}-flash.bin"
    saved = subprocess.run([programmer, "-c", "port=SWD", f"sn={serial}", "mode=HOTPLUG",
                            "-u", "0x08000000", hex(flash_size), str(backup)], capture_output=True, text=True)
    if not backup.is_file() or backup.stat().st_size != flash_size:
        report.check(False, f"{label} flash: save the board's flash before writing", saved.stdout[-300:])
        return
    data = backup.read_bytes()
    used = len(data.rstrip(b"\xff"))
    trimmed = backup.with_suffix(".trimmed.bin")
    trimmed.write_bytes(data[: (used + 31) // 32 * 32])
    print(f"      saved the {label}'s flash to {backup} ({used} bytes in use)", flush=True)
    try:
        flash = await server.call("flash_firmware", image_path=str(image), probe_serial=serial)
        report.check(flash["status"] == "verified" and "identical" in flash.get("verified_by", ""),
                     f"{label} flash: flash_firmware is verified by reading the range back", _why(flash))
        booted = await server.call("reset_target", probe_serial=serial)
        report.check(booted["status"] == "verified",
                     f"{label} flash: the written image's reset handler is where the core starts", _why(booted))
    finally:
        if used:
            restored = subprocess.run([programmer, *connect, "-w", str(trimmed), "0x08000000", "-v"],
                                      capture_output=True, text=True)
            report.check("Download verified successfully" in restored.stdout,
                         f"{label} flash: the board's own flash is written back and verified",
                         f"saved copy kept at {backup}")
        await server.call("reset_target", probe_serial=serial)


async def check_f4(server: Server, serial: str, images: Built, report: Report, console: bool) -> bool:
    """An STM32F407 board. Its images print on UART5 (PC12); with --f4-console
    naming a serial adapter there the RAM run is verified by console, and
    without one it is unverified and the counter shows it running."""
    s = serial
    board = await server.call("board_state", probe_serial=s)
    report.check(board["status"] == "verified" and board["family"]["name"] == "STM32F4"
                 and "access_port_warning" not in board,
                 "F4: board_state identifies the family on a measured port", _why(board))
    if not _answered(board, "F4"):
        return False
    run = await server.call("run_firmware", elf_path=str(images.elf["f4_ram"]), probe_serial=s, console_seconds=2)
    if console:
        report.check(run["status"] == "verified" and "f407 tick 0" in run.get("console", ""),
                     "F4: run_firmware is verified by the image's own console", _why(run))
    else:
        report.check(run["status"] == "unverified",
                     "F4: with no console, run_firmware is unverified, not verified", _why(run))
    await server.call("release_probe", probe_serial=s)
    first = await server.call("read_memory", address=F4_COUNTER, length_bytes=4, probe_serial=s)
    await anyio.sleep(1.0)
    second = await server.call("read_memory", address=F4_COUNTER, length_bytes=4, probe_serial=s)
    a, b = _counter(first, F4_COUNTER), _counter(second, F4_COUNTER)
    report.check(a is not None and b is not None and b > a,
                 "F4: the image runs (its counter advances)", f"{a and hex(a)} then {b and hex(b)}")
    elf = images.elf["f4_fault"]
    await server.call("run_firmware", elf_path=str(elf), probe_serial=s, console_seconds=2)
    await server.call("release_probe", probe_serial=s)
    await anyio.sleep(3.0)
    status = await server.call("read_fault_status", probe_serial=s)
    report.check(any(f["bit"] == "UNDEFINSTR" for f in status.get("faults", [])),
                 "F4: read_fault_status decodes the undefined instruction", _why(status))
    where = await server.call("locate_fault", probe_serial=s, elf_path=str(elf))
    report.check(where["status"] == "verified" and where.get("faulting_pc") == f"0x{_udf_address(elf):08X}",
                 "F4: locate_fault finds the udf", f"{_why(where)}; pc {where.get('faulting_pc')}")
    await server.call("run_firmware", elf_path=str(images.elf["f4_itm"]), probe_serial=s, console_seconds=1)
    await server.call("release_probe", probe_serial=s)
    swo = await server.call("capture_swo", probe_serial=s, core_clock_hz=16_000_000, seconds=3)
    report.check(swo["status"] == "verified" and "f407 itm tick" in swo.get("output", ""),
                 "F4: capture_swo reads ITM port 0 over SWO at the core's 16 MHz", _why(swo))
    double = await server.call("capture_swo", probe_serial=s, core_clock_hz=32_000_000, seconds=2)
    report.check(double["status"] == "unverified" and "32000000" in double.get("unverified_because", ""),
                 "F4: capture_swo at twice the core clock hears nothing and says the clock may be wrong", _why(double))
    await check_names(server, "F4", s, images, report, "f4_vars", "h7_vars", "0x40023800", "all 4 files")
    reset = await server.call("reset_target", probe_serial=s)
    report.check(reset["status"] == "verified", "F4: reset_target halts at the reset handler", _why(reset))
    return True


async def check_jtag(server: Server, serial: str, images: Built, report: Report) -> None:
    board = await server.call("board_state", probe_serial=serial, interface="jtag")
    report.check(board["status"] == "verified" and board.get("interface") == "jtag" and board.get("interface_clock_khz"),
                 "JTAG: board_state over JTAG reports the clock the banner gave",
                 f"{_why(board)}; {board.get('interface_clock_khz')} kHz")
    if not _answered(board, "JTAG"):
        return
    run = await server.call("run_firmware", elf_path=str(images.elf["h7_ram"]), probe_serial=serial,
                            console_seconds=1.5, interface="jtag")
    report.check(run["status"] == "verified" and run.get("console_port_source") == "STLINK_MCP_CONSOLE",
                 "JTAG: run_firmware over JTAG, verified on the console a setting assigns", _why(run))
    other = await server.call("run_firmware", elf_path=str(images.elf["h7_ram"]), probe_serial=serial, console_seconds=1)
    report.check(other["status"] == "failed" and "uses JTAG" in other.get("error", ""),
                 "JTAG: a server held over JTAG is not reused for SWD", _why(other))
    await server.call("release_probe", probe_serial=serial)


async def check_n6(server: Server, serial: str, images: Built, report: Report) -> bool:
    s = serial
    board = await server.call("board_state", probe_serial=s)
    report.check(board["status"] == "verified" and board["family"]["name"] == "STM32N6" and board.get("access_port") == 1,
                 "N6: board_state identifies the family on AP1", _why(board))
    if not _answered(board, "N6"):
        return False
    run = await server.call("run_firmware", elf_path=str(images.elf["n6_ram"]), probe_serial=s, console_seconds=1)
    report.check(run["status"] == "unverified", "N6: a silent image is unverified, not verified", _why(run))
    await server.call("release_probe", probe_serial=s)
    first = await server.call("read_memory", address=N6_COUNTER, length_bytes=4, probe_serial=s)
    await anyio.sleep(1.0)
    second = await server.call("read_memory", address=N6_COUNTER, length_bytes=4, probe_serial=s)
    a, b = _counter(first, N6_COUNTER), _counter(second, N6_COUNTER)
    report.check(a is not None and b is not None and b > a,
                 "N6: the image runs (its counter advances)", f"{a and hex(a)} then {b and hex(b)}")

    elf = images.elf["n6_fault"]
    await server.call("run_firmware", elf_path=str(elf), probe_serial=s, console_seconds=1)
    await server.call("release_probe", probe_serial=s)
    await anyio.sleep(2.0)
    status = await server.call("read_fault_status", probe_serial=s)
    report.check(any(f["bit"] == "UNDEFINSTR" for f in status.get("faults", [])) and "security_state_note" in status,
                 "N6: read_fault_status decodes the fault and notes the security banking", _why(status))
    where = await server.call("locate_fault", probe_serial=s, elf_path=str(elf))
    report.check(where["status"] == "verified" and where.get("faulting_pc") == f"0x{_udf_address(elf):08X}",
                 "N6: locate_fault finds the udf on its Secure stack",
                 f"{_why(where)}; pc {where.get('faulting_pc')}")
    return True


async def check_n6_closed(server: Server, serial: str, images: Built, report: Report) -> None:
    """Close the N6's debug port as ST's start-up code does, and check what the
    tools say to it. Only a power cycle reopens it."""
    s = serial
    await server.call("run_firmware", elf_path=str(images.elf["n6_close"]), probe_serial=s, console_seconds=1)
    await server.call("release_probe", probe_serial=s)
    read = await server.call("read_memory", address=N6_COUNTER, probe_serial=s)
    report.check(read["status"] == "failed" and read.get("target_was_reset") is not True
                 and "power cycle" in read.get("error", ""),
                 "N6 closed: read_memory fails without a reset and names the closed port", _why(read))
    board = await server.call("board_state", probe_serial=s)
    report.check(board["status"] == "unverified" and board.get("access_port") == 0
                 and board.get("target_was_reset") is True,
                 "N6 closed: board_state falls back to AP0 and says it tried a resetting connect", _why(board))
    run = await server.call("run_firmware", elf_path=str(images.elf["n6_ram"]), probe_serial=s, console_seconds=1)
    report.check(run["status"] == "failed" and "closed" in run.get("error", ""),
                 "N6 closed: run_firmware fails and names the closed port, not the cabling", _why(run))
    print("      The N6's debug port is closed now; power-cycle the board to reopen it.", flush=True)


async def check_together(server: Server, h7: str, n6: str, report: Report) -> None:
    started = time.monotonic()
    timings: dict[str, float] = {}

    async def timed(name: str, call: Callable[[], Awaitable[dict]]) -> None:
        begin = time.monotonic()
        await call()
        timings[name] = time.monotonic() - begin

    # Both sides take seconds: a read now takes one programmer call, and against a
    # tenth of a second the two orders cannot be told apart.
    async with anyio.create_task_group() as group:
        group.start_soon(timed, "h7", lambda: server.call("capture_console", probe_serial=h7, seconds=3))
        group.start_soon(timed, "n6", lambda: server.call("capture_console", probe_serial=n6, seconds=3))
    total = time.monotonic() - started
    report.check(total < timings["h7"] + timings["n6"] - 0.5,
                 "Both: calls on different probes run at the same time",
                 f"{total:.1f} s together against {timings['h7']:.1f} + {timings['n6']:.1f} s")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--h7", help="serial of the NUCLEO-H743ZI's own probe")
    parser.add_argument("--n6", help="serial of the NUCLEO-N657X0-Q's own probe")
    parser.add_argument("--jtag", help="serial of an ST-LINK on the H7's JTAG connector")
    parser.add_argument("--f4", help="serial of the ST-LINK on an STM32F407 board")
    parser.add_argument("--f4-console", help="serial port wired to that board's UART5 TX (PC12)")
    parser.add_argument("--n6-closed", action="store_true",
                        help="last, close the N6's debug port and check what the tools say; "
                             "the N6 then needs a power cycle")
    parser.add_argument("--flash", action="store_true",
                        help="also write, read back and restore the flash of the H7 and the F4")
    args = parser.parse_args()

    images = build(Path(tempfile.mkdtemp(prefix="stlink-hwcheck-")))
    report = Report()
    env = {"STLINK_MCP_CONSOLE": f"{args.f4}={args.f4_console}"} if args.f4 and args.f4_console else {}
    async with Server(env) as server:
        listed = await server.call("list_probes")
        by_board = {p["board"]: p for p in listed.get("probes", []) if p.get("board")}
        h7 = args.h7 or by_board.get(H7_BOARD, {}).get("serial")
        n6 = args.n6 or by_board.get(N6_BOARD, {}).get("serial")
        print(f"H7 probe {h7 or 'not found'}, N6 probe {n6 or 'not found'}\n", flush=True)
        h7_up = n6_up = False
        if h7:
            h7_up = await check_h7(server, h7, images, report)
            if h7_up and args.flash:
                await check_flash(server, "H7", h7, 0x200000, images.bin["h7_flash"], report)
        if n6:
            n6_up = await check_n6(server, n6, images, report)
        if h7_up and n6_up:
            await check_together(server, h7, n6, report)
        if n6_up and args.n6_closed:
            await check_n6_closed(server, n6, images, report)
        if args.f4:
            f4_up = await check_f4(server, args.f4, images, report, console=bool(args.f4_console))
            if f4_up and args.flash:
                await check_flash(server, "F4", args.f4, 0x100000, images.bin["f4_flash"], report)
    if args.jtag and h7:
        console = next((p["console_port"] for p in listed.get("probes", []) if p["serial"] == h7), None)
        env = {"STLINK_MCP_CONSOLE": f"{args.jtag}={console}"} if console else {}
        async with Server(env) as jtag_server:
            await check_jtag(jtag_server, args.jtag, images, report)
    if h7_up:
        async with Server() as server:
            await server.call("reset_target", probe_serial=h7)  # leave the H7 on its own firmware

    print(f"\n{len(report.rows) - report.failed} passed, {report.failed} failed")
    return 1 if report.failed or not report.rows else 0


if __name__ == "__main__":
    sys.exit(anyio.run(main))
