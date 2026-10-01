"""Check what the README promises against attached boards, through the server.

    uv run python tools/hwcheck/hwcheck.py [--h7 SERIAL] [--n6 SERIAL]
                                           [--jtag SERIAL] [--flash]

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
JTAG through it.

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
H7_COUNTER, N6_COUNTER = "0x24070000", "0x341B0000"


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
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._stack.aclose()

    async def call(self, tool: str, **args: object) -> dict:
        assert self.session is not None
        result = await self.session.call_tool(tool, args)
        if result.structured_content is not None:
            return result.structured_content
        return json.loads(result.content[0].text)


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[bool, str, str]] = []

    def check(self, ok: bool, name: str, saw: str) -> bool:
        self.rows.append((ok, name, saw))
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"\n        {saw}" if saw else ""), flush=True)
        return ok

    @property
    def failed(self) -> int:
        return sum(1 for ok, _, _ in self.rows if not ok)


def _why(result: dict) -> str:
    text = result.get("verified_by") or result.get("unverified_because") or result.get("error") or ""
    return f"{result.get('status')}: {text[:160]}"


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


async def check_h7(server: Server, serial: str, images: Built, report: Report) -> None:
    s = serial

    board = await server.call("board_state", probe_serial=s)
    report.check(
        board["status"] == "verified" and board["family"]["name"] == "STM32H7"
        and "access_port_warning" not in board,
        "H7: board_state identifies the family on a measured port", _why(board))

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

    reset = await server.call("reset_target", probe_serial=s)
    report.check(reset["status"] == "verified", "H7: reset_target halts at the reset handler the vector table names", _why(reset))
    idle = await server.call("release_probe", probe_serial=s)
    report.check(idle["status"] == "unverified", "H7: release_probe with nothing held is unverified", _why(idle))


async def check_h7_flash(server: Server, serial: str, images: Built, report: Report) -> None:
    """Write the H7's flash, read it back, and restore the board's own image."""
    programmer = str(find_toolchain().programmer)
    connect = ["-c", "port=SWD", f"sn={serial}", "mode=UR"]
    backup = Path(tempfile.mkdtemp(prefix="stlink-hwcheck-flash-")) / "h7-flash.bin"
    saved = subprocess.run([programmer, "-c", "port=SWD", f"sn={serial}", "mode=HOTPLUG",
                            "-u", "0x08000000", "0x200000", str(backup)], capture_output=True, text=True)
    if not backup.is_file() or backup.stat().st_size != 0x200000:
        report.check(False, "H7 flash: save the board's flash before writing", saved.stdout[-300:])
        return
    data = backup.read_bytes()
    used = len(data.rstrip(b"\xff"))
    trimmed = backup.with_suffix(".trimmed.bin")
    trimmed.write_bytes(data[: (used + 31) // 32 * 32])
    print(f"      saved the H7's flash to {backup} ({used} bytes in use)", flush=True)
    try:
        flash = await server.call("flash_firmware", image_path=str(images.bin["h7_flash"]), probe_serial=serial)
        report.check(flash["status"] == "verified" and "identical" in flash.get("verified_by", ""),
                     "H7 flash: flash_firmware is verified by reading the range back", _why(flash))
        booted = await server.call("reset_target", probe_serial=serial)
        report.check(booted["status"] == "verified", "H7 flash: the written image's reset handler is where the core starts", _why(booted))
    finally:
        restored = subprocess.run([programmer, *connect, "-w", str(trimmed), "0x08000000", "-v"],
                                  capture_output=True, text=True)
        report.check("Download verified successfully" in restored.stdout,
                     "H7 flash: the board's own flash is written back and verified",
                     f"saved copy kept at {backup}")
        await server.call("reset_target", probe_serial=serial)


async def check_jtag(server: Server, serial: str, images: Built, report: Report) -> None:
    board = await server.call("board_state", probe_serial=serial, interface="jtag")
    report.check(board["status"] == "verified" and board.get("interface") == "jtag" and board.get("interface_clock_khz"),
                 "JTAG: board_state over JTAG reports the clock the banner gave",
                 f"{_why(board)}; {board.get('interface_clock_khz')} kHz")
    run = await server.call("run_firmware", elf_path=str(images.elf["h7_ram"]), probe_serial=serial,
                            console_seconds=1.5, interface="jtag")
    report.check(run["status"] == "verified" and run.get("console_port_source") == "STLINK_MCP_CONSOLE",
                 "JTAG: run_firmware over JTAG, verified on the console a setting assigns", _why(run))
    other = await server.call("run_firmware", elf_path=str(images.elf["h7_ram"]), probe_serial=serial, console_seconds=1)
    report.check(other["status"] == "failed" and "uses JTAG" in other.get("error", ""),
                 "JTAG: a server held over JTAG is not reused for SWD", _why(other))
    await server.call("release_probe", probe_serial=serial)


async def check_n6(server: Server, serial: str, images: Built, report: Report) -> None:
    s = serial
    board = await server.call("board_state", probe_serial=s)
    report.check(board["status"] == "verified" and board["family"]["name"] == "STM32N6" and board.get("access_port") == 1,
                 "N6: board_state identifies the family on AP1", _why(board))
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


async def check_together(server: Server, h7: str, n6: str, report: Report) -> None:
    started = time.monotonic()
    timings: dict[str, float] = {}

    async def timed(name: str, call: Callable[[], Awaitable[dict]]) -> None:
        begin = time.monotonic()
        await call()
        timings[name] = time.monotonic() - begin

    async with anyio.create_task_group() as group:
        group.start_soon(timed, "h7", lambda: server.call("capture_console", probe_serial=h7, seconds=3))
        group.start_soon(timed, "n6", lambda: server.call("read_memory", address=N6_COUNTER, probe_serial=n6))
    total = time.monotonic() - started
    report.check(total < timings["h7"] + timings["n6"] - 0.5,
                 "Both: calls on different probes run at the same time",
                 f"{total:.1f} s together against {timings['h7']:.1f} + {timings['n6']:.1f} s")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--h7", help="serial of the NUCLEO-H743ZI's own probe")
    parser.add_argument("--n6", help="serial of the NUCLEO-N657X0-Q's own probe")
    parser.add_argument("--jtag", help="serial of an ST-LINK on the H7's JTAG connector")
    parser.add_argument("--flash", action="store_true", help="also write, read back and restore the H7's flash")
    args = parser.parse_args()

    images = build(Path(tempfile.mkdtemp(prefix="stlink-hwcheck-")))
    report = Report()
    async with Server() as server:
        listed = await server.call("list_probes")
        by_board = {p["board"]: p for p in listed.get("probes", []) if p.get("board")}
        h7 = args.h7 or by_board.get(H7_BOARD, {}).get("serial")
        n6 = args.n6 or by_board.get(N6_BOARD, {}).get("serial")
        print(f"H7 probe {h7 or 'not found'}, N6 probe {n6 or 'not found'}\n", flush=True)
        if h7:
            await check_h7(server, h7, images, report)
            if args.flash:
                await check_h7_flash(server, h7, images, report)
        if n6:
            await check_n6(server, n6, images, report)
        if h7 and n6:
            await check_together(server, h7, n6, report)
    if args.jtag and h7:
        console = next((p["console_port"] for p in listed.get("probes", []) if p["serial"] == h7), None)
        env = {"STLINK_MCP_CONSOLE": f"{args.jtag}={console}"} if console else {}
        async with Server(env) as jtag_server:
            await check_jtag(jtag_server, args.jtag, images, report)
    if h7:
        async with Server() as server:
            await server.call("reset_target", probe_serial=h7)  # leave the H7 on its own firmware

    print(f"\n{len(report.rows) - report.failed} passed, {report.failed} failed")
    return 1 if report.failed or not report.rows else 0


if __name__ == "__main__":
    sys.exit(anyio.run(main))
