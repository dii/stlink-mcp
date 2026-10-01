"""tools/hwcheck needs boards to run, but its images must stay buildable.

Builds every variant when STM32CubeCLT's compiler is installed, and checks
otherwise that every variant names a linker script that exists.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HWCHECK = Path(__file__).resolve().parent.parent / "tools" / "hwcheck"
sys.path.insert(0, str(HWCHECK))

import build_images  # noqa: E402


def test_every_variant_names_a_linker_script_that_exists() -> None:
    for image in build_images.VARIANTS:
        assert (build_images.IMAGES / image.linker_script).is_file(), image.name


def test_every_variant_builds(tmp_path) -> None:
    from stlink_mcp.toolchain import ToolchainError, find_toolchain
    try:
        gcc = find_toolchain().root / "GNU-tools-for-STM32" / "bin" / "arm-none-eabi-gcc"
    except ToolchainError:
        pytest.skip("no STM32CubeCLT install")
    if not gcc.exists():
        pytest.skip("no arm-none-eabi-gcc in the STM32CubeCLT install")
    built = build_images.build(tmp_path)
    assert set(built.elf) == {image.name for image in build_images.VARIANTS}
    assert built.bin["h7_flash"].stat().st_size > 64
