"""Locating and invoking the STM32CubeCLT command line tools."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    """CubeCLT tools colour their output even when not on a terminal."""
    return _ANSI.sub("", text)


class ToolchainError(RuntimeError):
    """The toolchain is missing or misconfigured. Always raised, never returned
    as a success with a warning, because a silently degraded toolchain is how
    you end up debugging the wrong layer."""


@dataclass(frozen=True)
class Toolchain:
    """Paths to the CubeCLT executables this server drives."""

    root: Path

    @property
    def programmer(self) -> Path:
        return self.root / "STM32CubeProgrammer" / "bin" / "STM32_Programmer_CLI"

    # No signing_tool property. STM32_SigningTool_CLI sits next to the
    # programmer, and it is tempting to expose it, but this project does not
    # sign: keys and header options belong to the project being built, not to a
    # programmer. A path nothing calls only suggests otherwise.

    @property
    def gdb_server(self) -> Path:
        return self.root / "STLink-gdb-server" / "bin" / "ST-LINK_gdbserver"

    @property
    def gdb(self) -> Path:
        return self.root / "GNU-tools-for-STM32" / "bin" / "arm-none-eabi-gdb"

    @property
    def addr2line(self) -> Path:
        """Turns an address into a function and source line, given the ELF."""
        return self.root / "GNU-tools-for-STM32" / "bin" / "arm-none-eabi-addr2line"

    @property
    def programmer_bin_dir(self) -> Path:
        """The GDB server needs this passed with -cp."""
        return self.root / "STM32CubeProgrammer" / "bin"

    def external_loader(self, name: str) -> Path:
        return self.programmer_bin_dir / "ExternalLoader" / name

    def check(self) -> None:
        """Fail loudly and specifically if anything required is missing."""
        missing = [
            str(p)
            for p in (self.programmer, self.gdb_server, self.gdb)
            if not p.exists()
        ]
        if missing:
            raise ToolchainError(
                "STM32CubeCLT is incomplete at {root}. Missing:\n  {items}\n"
                "Set STLINK_MCP_CLT_ROOT to your install, or install STM32CubeCLT.".format(
                    root=self.root, items="\n  ".join(missing)
                )
            )


def _version_key(path: Path) -> tuple[tuple[int, ...], str]:
    """Order install directories by version number, for a newest-first sort.

    This is not a tidiness change. The previous version sorted the directory
    names as STRINGS, which puts `stm32cubeclt_1.9.0` ahead of
    `stm32cubeclt_1.22.0`, because the comparison reaches '9' against '2' and
    stops. So `find_toolchain` returned the OLDEST install while its own
    docstring promised the newest, and nothing failed: the user simply ran a
    stale programmer against their board.

    The reason it is worth a named function and a test rather than a one-line
    fix is what happens next. Once the vendor device databases are read at
    runtime, picking the wrong install means seeding target addresses out of a
    stale database, with no signal to the agent that a newer one was skipped.

    Directories with no digits in the name sort last rather than crashing, and
    the name is a tiebreak so the order is stable rather than filesystem
    dependent.
    """
    return (tuple(int(n) for n in re.findall(r"\d+", path.name)), path.name)


_CLT_ROOT_ENV = "STLINK_MCP_CLT_ROOT"

#: Where an install is looked for, as stm32cubeclt* directories inside each.
#: "~" is expanded when searched; a machine with no home directory skips it.
_SEARCH_BASES = ("/opt/st", "~/st", "/usr/local/st")

#: A macOS location, searched as-is. Not tried on a Mac by this project.
_MAC_ROOT = Path("/Applications/STMicroelectronics/STM32CubeCLT")


def _candidate_roots() -> list[Path]:
    env = os.environ.get(_CLT_ROOT_ENV)
    if env:
        return [Path(env).expanduser()]

    roots: list[Path] = []
    for name in _SEARCH_BASES:
        try:
            base = Path(name).expanduser()
        except RuntimeError:
            # No home directory to expand "~" against, which some minimal
            # container launches produce. The other places are still searched.
            continue
        if base.is_dir():
            roots.extend(sorted(base.glob("stm32cubeclt*"), key=_version_key, reverse=True))
    if _MAC_ROOT.is_dir():
        roots.append(_MAC_ROOT)
    return roots


def find_toolchain() -> Toolchain:
    """Locate STM32CubeCLT, newest first when several are installed."""
    candidates = _candidate_roots()
    for root in candidates:
        tc = Toolchain(root=root)
        if tc.programmer.exists():
            return tc

    env = os.environ.get(_CLT_ROOT_ENV)
    if env:
        raise ToolchainError(
            f"{_CLT_ROOT_ENV} is {env}, and there is no "
            "STM32CubeProgrammer/bin/STM32_Programmer_CLI under it. Point it at "
            "the STM32CubeCLT install directory itself."
        )
    # Every place searched is named, not only the ones that existed. The
    # previous message fell back to "/opt/st, ~/st" when none existed, which
    # left out two of the four places the README says are searched.
    found = ", ".join(str(c) for c in candidates)
    raise ToolchainError(
        "No STM32CubeCLT install found. Looked for stm32cubeclt* directories in "
        f"{', '.join(_SEARCH_BASES)}, and at {_MAC_ROOT}"
        + (f"; found {found}, none with STM32_Programmer_CLI" if found else "")
        + f". Set {_CLT_ROOT_ENV} to the install directory."
    )


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run_command(argv: list[str], timeout: float = 120.0) -> CommandResult:
    """Run a toolchain command, merging stderr into stdout and stripping colour.

    Note the exit status of these tools is not trustworthy on its own. It is
    returned here for completeness, but callers should verify the effect they
    wanted rather than believe a zero.
    """
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolchainError(
            f"{argv[0]} did not finish within {timeout}s. "
            "The probe may be held by another process, such as a GDB server or an IDE."
        ) from exc
    except FileNotFoundError as exc:
        raise ToolchainError(f"Not found: {argv[0]}") from exc
    except PermissionError as exc:
        raise ToolchainError(f"Not executable by this user: {argv[0]}") from exc

    return CommandResult(
        argv=argv,
        returncode=proc.returncode,
        stdout=strip_ansi((proc.stdout or "") + (proc.stderr or "")),
    )
