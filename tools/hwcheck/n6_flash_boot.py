"""Does an STM32N6 booted from its external flash stay debuggable when its
first-stage boot loader opens debug, and does flash_firmware keep ST's loader
away from it there? A bench measurement in steps, each run with the switch where it says.

    uv run python tools/hwcheck/n6_flash_boot.py write         # BOOT1 in development boot
    uv run python tools/hwcheck/n6_flash_boot.py boot          # BOOT1 in flash boot, power cycled
    uv run python tools/hwcheck/n6_flash_boot.py refuse        # still flash boot
    uv run python tools/hwcheck/n6_flash_boot.py write-closed  # development boot, power cycled
    uv run python tools/hwcheck/n6_flash_boot.py closed        # flash boot, power cycled
    uv run python tools/hwcheck/n6_flash_boot.py recover       # development boot, power cycled

`refuse` checks that flash_firmware will not run ST's loader in flash boot,
which on 2026-10-06 hung the core until a power cycle (docs/findings.md), and
that a reset brings the image back with debug open. The `closed` pair is the
control: an image that does not open debug keeps the probe out.

Before each step, say where the switch is; each step ends by saying what to
do next. The images are build_images.py's n6_boot_open, n6_boot_open_b and
n6_boot_closed, each with ST's boot header added with no keys. They print their
boot state on the board's virtual COM port: the BSEC state, the boot level, the
two debug registers after any write, and the boot pins.

What it changes on the board: `write` saves the first 128 KB of the external
flash to the folder given by --keep (only if no copy is there yet) and then
writes the boot images over it; `recover` writes the saved copy back.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import anyio

sys.path.insert(0, str(Path(__file__).parent))
from build_images import build  # noqa: E402
from hwcheck import N6_BOARD, N6_COUNTER, N6_LOADER, Report, Server, _counter, _why  # noqa: E402

from stlink_mcp.toolchain import find_toolchain  # noqa: E402

BASE, KEPT = 0x70000000, 0x20000
BSEC = {"bsec_sr": 0x56009E40, "dbgcr": 0x56009E8C, "ap_unlock": 0x56009E90, "hdpl": 0x56009E94,
        "boot_pins": 0x56008100}
NEXT = {
    "write": "Set BOOT1 to flash boot (low), power-cycle the board, then run: boot",
    "boot": "Leave BOOT1 in flash boot, then run: refuse",
    "refuse": "For the control, set BOOT1 to development boot (high), power-cycle, run: write-closed; "
              "or, to finish, the same and run: recover",
    "write-closed": "Set BOOT1 to flash boot (low), power-cycle the board, then run: closed",
    "closed": "Set BOOT1 to development boot (high), power-cycle the board, then run: recover",
    "recover": "Leave BOOT1 in development boot, where tools/hwcheck expects it.",
}


async def _serial(server: Server, given: str | None) -> str | None:
    if given:
        return given
    listed = await server.call("list_probes")
    found = [p["serial"] for p in listed.get("probes", []) if p.get("board") == N6_BOARD]
    print(f"      list_probes: {_why(listed)}; N6 probes {found}", flush=True)
    return found[0] if len(found) == 1 else None


async def _registers(server: Server, serial: str) -> dict[str, str]:
    """The boot and debug registers as the probe reads them, or why it could not."""
    seen: dict[str, str] = {}
    for name, address in BSEC.items():
        out = await server.call("read_memory", address=f"0x{address:08X}", length_bytes=4, probe_serial=serial)
        word = out.get("words", {}).get(f"0x{address:08X}")
        seen[name] = word if word else f"not read ({out.get('status')})"
    print(f"      registers through the probe: {seen}", flush=True)
    return seen


async def _console(server: Server, serial: str, seconds: float = 4.0) -> str:
    out = await server.call("capture_console", probe_serial=serial, seconds=seconds)
    text = (out.get("console_buffered") or "") + (out.get("console") or "")
    for line in text.splitlines():
        if "bsec_sr" in line:
            print(f"      the image says: {line.strip()}", flush=True)
            break
    return text


def _ticks(text: str, tag: str) -> list[int]:
    return [int(n) for n in re.findall(rf"{re.escape(tag)} tick (\d+)", text)]


async def _runs(server: Server, serial: str) -> tuple[int | None, int | None]:
    first = await server.call("read_memory", address=N6_COUNTER, length_bytes=4, probe_serial=serial)
    await anyio.sleep(1.0)
    second = await server.call("read_memory", address=N6_COUNTER, length_bytes=4, probe_serial=serial)
    return _counter(first, N6_COUNTER), _counter(second, N6_COUNTER)


async def write(server: Server, serial: str, boot: dict[str, Path], keep: Path, report: Report,
                image: str = "n6_boot_open") -> None:
    await server.call("release_probe", probe_serial=serial)
    board = await server.call("board_state", probe_serial=serial)
    report.check(board["status"] == "verified" and board.get("access_port") == 1,
                 "development boot: board_state reaches the core on AP1", _why(board))
    await _registers(server, serial)
    backup = keep / "n6-flash-first-128k.bin"
    if not backup.exists():
        from stlink_mcp.probe import Probe
        probe = Probe(find_toolchain(), serial=serial)
        if not probe.identify(allow_reset=False).connected:
            report.check(False, "development boot: save the first 128 KB of flash", "no connect")
            return
        backup.write_bytes(probe.read_back(BASE, KEPT, N6_LOADER))
        print(f"      saved the first 128 KB of the external flash to {backup}", flush=True)
    report.check(board.get("boot_mode") == "development", "development boot: board_state reads it from the boot pins",
                 f"boot_mode {board.get('boot_mode')}")
    out = await server.call("flash_firmware", image_path=str(boot[image]), probe_serial=serial)
    report.check(out["status"] == "verified", f"development boot: {image} is written at 0x70000000", _why(out))


async def boot_step(server: Server, serial: str, report: Report) -> None:
    text = await _console(server, serial)
    ticks = _ticks(text, "n6 boot open A")
    report.check(bool(ticks), "flash boot: the boot ROM runs the image with a keyless header (its console)",
                 f"ticks {ticks[:3]}..." if ticks else f"heard {text[:160]!r}")
    board = await server.call("board_state", probe_serial=serial)
    report.check(board["status"] == "verified" and board.get("access_port") == 1,
                 "flash boot: board_state reaches the core the image opened",
                 f"{_why(board)}; target_was_reset {board.get('target_was_reset')}")
    report.check(board.get("boot_mode") == "flash", "flash boot: board_state reads it from the boot pins",
                 f"boot_mode {board.get('boot_mode')}")
    a, b = await _runs(server, serial)
    report.check(a is not None and b is not None and b > a, "flash boot: the counter advances, read by the probe",
                 f"{a and hex(a)} then {b and hex(b)}")
    await _registers(server, serial)


async def refuse(server: Server, serial: str, boot: dict[str, Path], report: Report) -> None:
    out = await server.call("flash_firmware", image_path=str(boot["n6_boot_open_b"]), probe_serial=serial)
    report.check(out["status"] == "failed" and out.get("boot_mode") == "flash",
                 "flash boot: flash_firmware refuses before ST's loader runs", _why(out))
    a, b = await _runs(server, serial)
    report.check(a is not None and b is not None and b > a, "flash boot: the image still runs after the refusal",
                 f"{a and hex(a)} then {b and hex(b)}")
    reset = await server.call("reset_target", probe_serial=serial)
    print(f"      reset_target: {_why(reset)}", flush=True)
    text = await _console(server, serial)
    ticks = _ticks(text, "n6 boot open A")
    report.check(bool(ticks) and min(ticks) < 50, "flash boot: after the reset the image boots again (its console)",
                 f"ticks {ticks[:3]}..." if ticks else f"heard {text[:160]!r}")
    board = await server.call("board_state", probe_serial=serial)
    report.check(board["status"] == "verified" and board.get("access_port") == 1,
                 "flash boot: the probe reaches the core again after the reset", _why(board))


async def closed(server: Server, serial: str, report: Report) -> None:
    text = await _console(server, serial)
    ticks = _ticks(text, "n6 boot closed")
    report.check(bool(ticks), "flash boot: it boots (its console)",
                 f"ticks {ticks[:3]}..." if ticks else f"heard {text[:160]!r}")
    board = await server.call("board_state", probe_serial=serial)
    report.check(board["status"] != "verified",
                 "flash boot: without the BSEC writes the probe does not reach the core (the control)",
                 f"{_why(board)}; device_id {board.get('device_id')}; access_port {board.get('access_port')}")


async def recover(server: Server, serial: str, keep: Path, report: Report) -> None:
    board = await server.call("board_state", probe_serial=serial)
    report.check(board["status"] == "verified" and board.get("access_port") == 1,
                 "development boot: the board is reachable again", _why(board))
    backup = keep / "n6-flash-first-128k.bin"
    if not backup.exists():
        report.check(False, "development boot: the saved flash is written back", f"no copy at {backup}")
        return
    from stlink_mcp.probe import Probe
    probe = Probe(find_toolchain(), serial=serial)
    probe.identify(allow_reset=False)
    probe.program_flash(str(backup), BASE, N6_LOADER)
    restored = probe.read_back(BASE, KEPT, N6_LOADER) == backup.read_bytes()
    report.check(restored, "development boot: the saved 128 KB are written back and read back identical",
                 "identical" if restored else "differs")


async def _run(args: argparse.Namespace) -> int:
    report = Report()
    out = Path(args.keep) / "images"
    built = build(out)
    boot = {name: built.bin[f"{name}-boot"] for name in ("n6_boot_open", "n6_boot_open_b", "n6_boot_closed")}
    async with Server() as server:
        serial = await _serial(server, args.n6)
        if serial is None:
            print("No single NUCLEO-N657X0-Q found; name its probe with --n6.", flush=True)
            return 1
        if args.step == "write":
            await write(server, serial, boot, Path(args.keep), report)
        elif args.step == "boot":
            await boot_step(server, serial, report)
        elif args.step == "refuse":
            await refuse(server, serial, boot, report)
        elif args.step == "write-closed":
            await write(server, serial, boot, Path(args.keep), report, image="n6_boot_closed")
        elif args.step == "closed":
            await closed(server, serial, report)
        else:
            await recover(server, serial, Path(args.keep), report)
    print(f"\n{len(report.rows) - report.failed} of {len(report.rows)} as predicted. Next: {NEXT[args.step]}", flush=True)
    return 1 if report.failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("step", choices=list(NEXT))
    parser.add_argument("--n6", help="serial of the NUCLEO-N657X0-Q's own probe")
    parser.add_argument("--keep", required=True,
                        help="folder for the built images and the saved flash, kept between steps")
    args = parser.parse_args()
    Path(args.keep).mkdir(parents=True, exist_ok=True)
    return anyio.run(_run, args)


if __name__ == "__main__":
    sys.exit(main())
