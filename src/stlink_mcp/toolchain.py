"""Locating and invoking the STM32CubeCLT command line tools."""

from __future__ import annotations

import os
import re
import shutil
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


#: What the executables end in: ".exe" on Windows, nothing elsewhere.
_EXE = ".exe" if os.name == "nt" else ""


#: What a Toolchain from an STM32CubeIDE install says it is, in --check.
CUBEIDE = "STM32CubeIDE"


@dataclass(frozen=True)
class Toolchain:
    """Paths to the CubeCLT executables this server drives.

    An STM32CubeCLT install keeps the three tools in fixed folders under its
    root. STM32CubeIDE carries the same tools, each in a plugin folder of its
    own whose name changes with every release, so a toolchain from it names
    those folders and leaves `root` as the CubeIDE install."""

    root: Path
    passed_over: tuple[Path, ...] = ()
    """Newer installs skipped because they lack one of the three executables."""
    also_incomplete: tuple[Path, ...] = ()
    """Other installs found when none was complete, for the error to name."""
    source: str = "STM32CubeCLT"
    programmer_home: Path | None = None
    gdb_server_home: Path | None = None
    gnu_home: Path | None = None

    @property
    def complete(self) -> bool:
        return all(p.exists() for p in (self.programmer, self.gdb_server, self.gdb))

    @property
    def programmer(self) -> Path:
        return self.programmer_bin_dir / f"STM32_Programmer_CLI{_EXE}"

    @property
    def gnu_bin_dir(self) -> Path:
        """The Arm GNU tools: GDB, addr2line, objdump, and gcc for the bench images."""
        return (self.gnu_home or self.root / "GNU-tools-for-STM32") / "bin"

    # No signing_tool property. STM32_SigningTool_CLI sits next to the
    # programmer, and it is tempting to expose it, but this project does not
    # sign: keys and header options belong to the project being built, not to a
    # programmer. A path nothing calls only suggests otherwise.

    @property
    def gdb_server(self) -> Path:
        return (self.gdb_server_home or self.root / "STLink-gdb-server") / "bin" / f"ST-LINK_gdbserver{_EXE}"

    @property
    def gdb(self) -> Path:
        return self.gnu_bin_dir / f"arm-none-eabi-gdb{_EXE}"

    @property
    def addr2line(self) -> Path:
        """Turns an address into a function and source line, given the ELF."""
        return self.gnu_bin_dir / f"arm-none-eabi-addr2line{_EXE}"

    @property
    def objdump(self) -> Path:
        """Disassembles the ELF, for the instruction at a fault or a stop."""
        return self.gnu_bin_dir / f"arm-none-eabi-objdump{_EXE}"

    @property
    def stlink_server(self) -> Path | None:
        """ST's probe-sharing daemon, which CubeCLT's installer puts on the PATH
        (/usr/bin on Linux) rather than in its own directory. With it, a GDB
        server started with -t lets the programmer connect with `shared`
        beside it; without it, a held server keeps the probe to itself."""
        found = shutil.which("stlink-server")
        return Path(found) if found else None

    @property
    def programmer_bin_dir(self) -> Path:
        """The GDB server needs this passed with -cp."""
        return (self.programmer_home or self.root / "STM32CubeProgrammer") / "bin"

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
            others = (
                "\nThe other installs found are incomplete too: "
                + ", ".join(str(p) for p in self.also_incomplete) + "."
                if self.also_incomplete else ""
            )
            raise ToolchainError(
                "{source} is incomplete at {root}. Missing:\n  {items}{others}\n"
                "Set STLINK_MCP_CLT_ROOT to your install, or install STM32CubeCLT.".format(
                    source=self.source, root=self.root, items="\n  ".join(missing), others=others
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
#: On Windows only C:\ST, which is where the installer is expected to put it.
_SEARCH_BASES = ("C:/ST",) if os.name == "nt" else ("/opt/st", "~/st", "/usr/local/st")

#: A macOS location, searched as-is. Not tried on a Mac by this project, and
#: not searched or named on Windows.
_MAC_ROOT: Path | None = None if os.name == "nt" else Path("/Applications/STMicroelectronics/STM32CubeCLT")


#: The plugin folders STM32CubeIDE keeps the three tools in, each holding a
#: `tools` folder laid out like the matching CubeCLT one. Names as one user's
#: STM32CubeIDE 2.0.0 on Linux had them (2026-10-27), for example
#: com.st.stm32cube.ide.mcu.externaltools.cubeprogrammer.linux64_2.2.300.202508131133;
#: the platform and the version stamp after the prefix change, so they are
#: matched by prefix and the newest complete one is taken.
_CUBEIDE_PLUGINS = (
    "com.st.stm32cube.ide.mcu.externaltools.cubeprogrammer.",
    "com.st.stm32cube.ide.mcu.externaltools.stlink-gdb-server.",
    "com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.",
)


def _cubeide_toolchain(install: Path) -> Toolchain | None:
    """The tools inside an STM32CubeIDE install, or None when any is missing.

    The plugins are in `plugins` under the install on Linux, and in
    `STM32CubeIDE/plugins` under it where the installer adds a level, as on
    Windows; both are looked at. An install updated in place can keep older
    plugin folders beside the new ones, so each tool comes from its newest
    folder that has the executable."""
    plugins = next((d for d in (install / "plugins", install / "STM32CubeIDE" / "plugins")
                    if d.is_dir()), None)
    if plugins is None:
        return None
    wanted = (
        lambda home: Toolchain(root=install, programmer_home=home).programmer,
        lambda home: Toolchain(root=install, gdb_server_home=home).gdb_server,
        lambda home: Toolchain(root=install, gnu_home=home).gdb,
    )
    homes: list[Path] = []
    for prefix, executable in zip(_CUBEIDE_PLUGINS, wanted):
        folders = sorted(plugins.glob(prefix + "*"), key=_version_key, reverse=True)
        home = next((f / "tools" for f in folders if executable(f / "tools").exists()), None)
        if home is None:
            return None
        homes.append(home)
    return Toolchain(root=install, source=CUBEIDE, programmer_home=homes[0],
                     gdb_server_home=homes[1], gnu_home=homes[2])


def _cubeide_installs() -> list[Path]:
    """stm32cubeide* directories in the places CubeCLT is looked for, newest
    first. Windows matches the name without regard to case."""
    installs: list[Path] = []
    for name in _SEARCH_BASES:
        try:
            base = Path(name).expanduser()
        except RuntimeError:
            continue
        if base.is_dir():
            installs.extend(sorted(base.glob("stm32cubeide*"), key=_version_key, reverse=True))
    return installs


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
    if _MAC_ROOT is not None and _MAC_ROOT.is_dir():
        roots.append(_MAC_ROOT)
    return roots


def find_toolchain() -> Toolchain:
    """Locate STM32CubeCLT: the newest complete install when several are there,
    and with none complete, the tools inside the newest complete STM32CubeIDE.

    Complete means the programmer, the GDB server and GDB all exist. Taking the
    newest install that merely had a programmer chose a broken newer install
    over a working older one, and the error then named only the broken one
    (red-team RT9, 2026-09-03)."""
    candidates = _candidate_roots()
    usable = [Toolchain(root=root) for root in candidates if Toolchain(root=root).programmer.exists()]
    for index, tc in enumerate(usable):
        if tc.complete:
            return Toolchain(root=tc.root, passed_over=tuple(t.root for t in usable[:index]))

    # No complete CubeCLT. STM32CubeIDE carries the same three tools, and a
    # machine with only the IDE used to need a folder of links made to look
    # like CubeCLT (a user's report, 2026-10-27).
    env = os.environ.get(_CLT_ROOT_ENV)
    for install in ([Path(env).expanduser()] if env else _cubeide_installs()):
        ide = _cubeide_toolchain(install)
        if ide is not None:
            return Toolchain(root=ide.root, source=CUBEIDE, programmer_home=ide.programmer_home,
                             gdb_server_home=ide.gdb_server_home, gnu_home=ide.gnu_home,
                             passed_over=tuple(t.root for t in usable))

    if usable:
        return Toolchain(root=usable[0].root, also_incomplete=tuple(t.root for t in usable[1:]))

    if env:
        raise ToolchainError(
            f"{_CLT_ROOT_ENV} is {env}, and there is no "
            f"STM32CubeProgrammer/bin/STM32_Programmer_CLI{_EXE} under it, nor an "
            "STM32CubeIDE plugins folder with the programmer, the GDB server and GDB. "
            "Point it at the STM32CubeCLT or STM32CubeIDE install directory itself."
        )
    # Every place searched is named, not only the ones that existed. The
    # previous message fell back to "/opt/st, ~/st" when none existed, which
    # left out two of the four places the README says are searched.
    found = ", ".join(str(c) for c in candidates)
    raise ToolchainError(
        "No STM32CubeCLT install found. Looked for stm32cubeclt* directories in "
        f"{', '.join(str(Path(b)) for b in _SEARCH_BASES)}"
        + (f", and at {_MAC_ROOT}" if _MAC_ROOT is not None else "")
        + (f"; found {found}, none with STM32_Programmer_CLI" if found else "")
        + ", nor a complete STM32CubeIDE in stm32cubeide* directories in the same places"
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
