"""Build the hardware-check images with the Arm compiler STM32CubeCLT includes.

    uv run python tools/hwcheck/build_images.py [OUT_DIR]

Each image is images/beacon.c, or images/debug_target.c for the ones with
variables to read by name, built with a set of -D flags and a linker script. They are built fresh into a directory outside the repository on every
run of hwcheck.py, so no build output is ever tracked or packaged.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from stlink_mcp.toolchain import find_toolchain

IMAGES = Path(__file__).parent / "images"

_COMMON = [
    "-mthumb", "-O1", "-g", "-fno-tree-vectorize", "-ffreestanding",
    "-nostdlib", "-nostartfiles", "-Wall", "-Werror",
    "-Wl,--build-id=none", "-Wl,-n",
    # Built from inside images/, so the debug information names its sources as
    # images/<file> and carries no path from the machine that built it.
    f"-fdebug-prefix-map={Path(__file__).parent / 'images'}=images",
]
_H7 = ["-mcpu=cortex-m7", "-DHAS_UART", "-DCOUNTER_ADDR=0x24070000u"]
_N6 = ["-mcpu=cortex-m55", "-DCOUNTER_ADDR=0x341B0000u", "-DDELAY=300000"]
_F4 = ["-mcpu=cortex-m4", "-DF4_UART5", "-DCOUNTER_ADDR=0x2001F000u", "-DDELAY=1000000"]
_SOFT = ["-mfloat-abi=soft"]
_FAULT = ["-DSET_VTOR", "-DFAULT_AFTER=3"]


@dataclass(frozen=True)
class Image:
    name: str
    flags: list[str]
    linker_script: str
    binary: bool = False
    """Also produce a raw .bin with objcopy, for flash_firmware."""
    notes: str = ""
    source: str = "beacon.c"


VARIANTS: list[Image] = [
    Image("h7_ram", _H7 + _SOFT, "h7_ram.ld", notes="prints and counts in AXI SRAM"),
    Image("h7_silent", ["-mcpu=cortex-m7", "-DCOUNTER_ADDR=0x24070000u"] + _SOFT, "h7_ram.ld",
          notes="counts and prints nothing"),
    Image("h7_fault", _H7 + _SOFT + _FAULT + ['-DTAG="fault"'], "h7_ram.ld",
          notes="udf on the main stack after four lines"),
    Image("h7_fault_psp", _H7 + _SOFT + _FAULT + ["-DUSE_PSP", '-DTAG="psp"'], "h7_ram.ld",
          notes="switches to PSP, then udf"),
    Image("h7_fault_fp", _H7 + ["-mfloat-abi=softfp", "-mfpu=fpv5-d16"] + _FAULT
          + ["-DUSE_FP", '-DTAG="fp"'], "h7_ram.ld", notes="FP context active, then udf"),
    Image("h7_fault_lr", _H7 + _SOFT + _FAULT + ["-DCALLING_HANDLER", '-DTAG="lr"'], "h7_ram.ld",
          notes="its HardFault handler calls a function first, overwriting LR"),
    Image("h7_busfault", _H7 + _SOFT + _FAULT + ["-DBUS_FAULT", '-DTAG="bus"'], "h7_ram.ld",
          notes="a store to 0x2C000000: an imprecise bus error"),
    Image("h7_flashlinked", _H7 + _SOFT, "h7_flash.ld", notes="linked for flash; run_firmware refuses it"),
    Image("h7_mixed", _H7 + _SOFT + ["-DEXTRA_SEGMENT"], "h7_mixed.ld",
          notes="RAM vector table plus a flash segment; refused"),
    Image("h7_hole", _H7 + _SOFT + ["-DEXTRA_SEGMENT"], "h7_hole.ld",
          notes="a segment at 0x2C000000; GDB's load fails"),
    Image("h7_flash", _H7 + _SOFT + ['-DTAG="flash"'], "h7_flash.ld", binary=True,
          notes="boots from flash at 0x08000000"),
    Image("h7_itm", _H7 + _SOFT + ["-DITM_OUT", '-DTAG="itm"'], "h7_ram.ld",
          notes="prints on USART3 and on ITM port 0, for SWO"),
    Image("f4_ram", _F4 + _SOFT + ['-DTAG="f407"'], "f4_ram.ld", notes="prints on UART5 and counts in SRAM1"),
    Image("f4_fault", _F4 + _SOFT + _FAULT + ['-DTAG="f407 fault"'], "f4_ram.ld", notes="udf after four ticks"),
    Image("f4_flash", _F4 + _SOFT + ['-DTAG="f407 flash"'], "f4_flash.ld", binary=True,
          notes="boots from flash at 0x08000000"),
    Image("f4_itm", _F4 + _SOFT + ["-DITM_OUT", '-DTAG="f407 itm"'], "f4_ram.ld",
          notes="prints on UART5 and on ITM port 0, for SWO"),
    Image("h7_vars", ["-mcpu=cortex-m7"] + _SOFT, "h7_data.ld", source="debug_target.c",
          notes="typed globals, a watched variable, a call chain; prints nothing"),
    Image("h7_vars_hang", ["-mcpu=cortex-m7", "-DHANG"] + _SOFT, "h7_data.ld", source="debug_target.c",
          notes="hangs three calls deep"),
    Image("f4_vars", ["-mcpu=cortex-m4", "-DDELAY=500000"] + _SOFT, "f4_data.ld", source="debug_target.c",
          notes="typed globals, a watched variable, a call chain; prints nothing"),
    Image("f4_vars_hang", ["-mcpu=cortex-m4", "-DDELAY=500000", "-DHANG"] + _SOFT, "f4_data.ld",
          source="debug_target.c", notes="hangs three calls deep"),
    Image("n6_vars", ["-mcpu=cortex-m55", "-DDELAY=300000"] + _SOFT, "n6_data.ld", source="debug_target.c",
          notes="typed globals, a watched variable, a call chain; prints nothing"),
    Image("n6_ram", _N6 + _SOFT, "n6_ram.ld", notes="counts in AXISRAM2, prints nothing"),
    Image("n6_fault", _N6 + _SOFT + _FAULT, "n6_ram.ld", notes="udf after four ticks"),
    Image("n6_close", _N6 + _SOFT + ["-DCLOSE_AP1"], "n6_ram.ld",
          notes="turns the SYSCFG clock off as ST's start-up code does, closing AP1 until a power cycle"),
]


@dataclass
class Built:
    elf: dict[str, Path] = field(default_factory=dict)
    bin: dict[str, Path] = field(default_factory=dict)


def build(out_dir: Path) -> Built:
    toolchain = find_toolchain()
    gnu = toolchain.root / "GNU-tools-for-STM32" / "bin"
    gcc, objcopy = gnu / "arm-none-eabi-gcc", gnu / "arm-none-eabi-objcopy"
    if not gcc.exists():
        raise SystemExit(f"No arm-none-eabi-gcc under {gnu}; STM32CubeCLT includes one.")
    out_dir.mkdir(parents=True, exist_ok=True)
    built = Built()
    for image in VARIANTS:
        elf = out_dir / f"{image.name}.elf"
        subprocess.run(
            [str(gcc), *_COMMON, *image.flags, f"-Wl,-T,{IMAGES / image.linker_script}",
             "-o", str(elf.resolve()), image.source],
            check=True, cwd=IMAGES,
        )
        built.elf[image.name] = elf
        if image.binary:
            raw = out_dir / f"{image.name}.bin"
            subprocess.run([str(objcopy), "-O", "binary", str(elf), str(raw)], check=True)
            built.bin[image.name] = raw
    return built


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="stlink-hwcheck-"))
    result = build(target)
    for name, path in result.elf.items():
        print(path)
    for name, path in result.bin.items():
        print(path)
