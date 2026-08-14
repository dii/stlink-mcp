"""Talking to the target through STM32_Programmer_CLI.

Everything here parses the CLI's human-readable output, which is the only
interface it offers. The parsers are deliberately strict: if the expected field
is absent we say so, rather than defaulting and reporting a confident wrong
answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .quirks import FamilyQuirks, for_device_id
from .toolchain import Toolchain, run_command


class ProbeError(RuntimeError):
    pass


@dataclass
class TargetInfo:
    """What the probe can tell us about the attached target."""

    connected: bool
    stlink_serial: str | None = None
    stlink_firmware: str | None = None
    board: str | None = None
    device_id: int | None = None
    device_name: str | None = None
    cpu: str | None = None
    revision: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def quirks(self) -> FamilyQuirks:
        return for_device_id(self.device_id)


_FIELDS = {
    "stlink_serial": re.compile(r"^ST-LINK SN\s*:\s*(\S+)", re.M),
    "stlink_firmware": re.compile(r"^ST-LINK FW\s*:\s*(\S+)", re.M),
    "board": re.compile(r"^Board\s*:\s*(.+?)\s*$", re.M),
    "device_name": re.compile(r"^Device name\s*:\s*(.+?)\s*$", re.M),
    "cpu": re.compile(r"^Device CPU\s*:\s*(.+?)\s*$", re.M),
    "revision": re.compile(r"^Revision ID\s*:\s*(.+?)\s*$", re.M),
}
_DEVICE_ID = re.compile(r"^Device ID\s*:\s*0x([0-9A-Fa-f]+)", re.M)
_ERROR = re.compile(r"^Error:\s*(.+?)\s*$", re.M)

# "0x34180400 : 34200000 34180841 3418089D 3418089D"
_MEM_LINE = re.compile(r"^0x([0-9A-Fa-f]+)\s*:\s*((?:[0-9A-Fa-f]{8}\s*)+)$", re.M)

# "PC       = 0x34180D3C"
_CORE_REG = re.compile(r"^([A-Z][A-Z0-9_]*)\s*=\s*0x([0-9A-Fa-f]+)", re.M)


def parse_target_info(output: str) -> TargetInfo:
    """Parse a connect banner. Exposed separately so it can be tested without
    hardware attached, which is most of the time."""
    info = TargetInfo(connected=False)

    for attr, pattern in _FIELDS.items():
        match = pattern.search(output)
        if match:
            setattr(info, attr, match.group(1))

    dev = _DEVICE_ID.search(output)
    if dev:
        info.device_id = int(dev.group(1), 16)

    # Deduplicate: the CLI retries the connection and repeats every error.
    seen: list[str] = []
    for err in _ERROR.findall(output):
        if err not in seen:
            seen.append(err)
    info.errors = seen

    info.connected = info.device_id is not None and not any(
        "Unable to get core ID" in e or "No STM32 target found" in e for e in seen
    )
    return info


def parse_memory(output: str) -> dict[int, int]:
    """Parse '-r32' output into {address: word}."""
    words: dict[int, int] = {}
    for match in _MEM_LINE.finditer(output):
        base = int(match.group(1), 16)
        for index, token in enumerate(match.group(2).split()):
            words[base + index * 4] = int(token, 16)
    return words


def parse_core_registers(output: str) -> dict[str, int]:
    """Parse '-coreReg' output into {register: value}."""
    return {name: int(value, 16) for name, value in _CORE_REG.findall(output)}


def explain_connect_failure(info: TargetInfo) -> str:
    """Turn an unhelpful CLI error into the two things it usually means.

    This exists because 'Unable to get core ID' is emitted for both a wrong
    access port and a boot configuration that closes the debug port, and those
    have completely different fixes.
    """
    if info.connected:
        return ""
    hints = [
        "The debug access port may be wrong. Some families do not use AP0 "
        "(the STM32N6 uses AP1); see quirks.py.",
        "The target's boot configuration may close the debug port. On parts "
        "that boot from external memory, the debug port is typically only open "
        "in the development boot mode selected by a BOOT pin or jumper.",
        "The probe may be held by another process, such as an IDE or a GDB server.",
    ]
    return "Could not reach the core. Most likely causes:\n  - " + "\n  - ".join(hints)


class Probe:
    """A thin, verified wrapper over STM32_Programmer_CLI."""

    def __init__(self, toolchain: Toolchain, access_port: int | None = None) -> None:
        self.toolchain = toolchain
        self._access_port = access_port
        self.degraded_reason: str | None = None
        """Set when the access port in use is not the one the family wants.

        Kept as state rather than raised, because identification still works in
        that situation and the caller should get the identity plus the warning,
        not an exception instead of both.
        """

    def _connect_args(self, access_port: int) -> list[str]:
        return [
            str(self.toolchain.programmer),
            "-c",
            "port=SWD",
            "mode=HOTPLUG",
            f"ap={access_port}",
        ]

    def identify(self) -> TargetInfo:
        """Identify the target and settle on the right debug access port.

        Two steps, and the second is not optional. Scanning finds a port that can
        read the device identity, but **identity is readable on a port that
        cannot halt the core**. On the STM32N6 the default AP0 reports device ID,
        name, CPU and revision quite happily, and then every attempt to halt
        fails with "Failed to halt target", which reads like broken hardware
        rather than a wrong flag.

        So once the family is known, its recorded access port wins over whichever
        port happened to answer first.
        """
        ports = [self._access_port] if self._access_port is not None else [0, 1]
        last = TargetInfo(connected=False)

        for port in ports:
            result = run_command(self._connect_args(port), timeout=90)
            info = parse_target_info(result.stdout)
            if info.connected:
                self._access_port = self._settle_access_port(info, port)
                return info
            last = info

        return last

    def _settle_access_port(self, info: TargetInfo, answered_on: int) -> int:
        """Prefer the family's recorded access port over the one that answered.

        Verified before adopting: if the recorded port cannot even identify the
        target then the table is wrong for this board, or the port has been
        closed, and staying on a port that demonstrably works beats trusting the
        table. Either way the degradation is recorded rather than hidden, because
        core control will then fail and the reason needs to be visible.
        """
        wanted = info.quirks.access_port
        if wanted == answered_on:
            return answered_on

        result = run_command(self._connect_args(wanted), timeout=90)
        if parse_target_info(result.stdout).connected:
            return wanted

        self.degraded_reason = (
            f"{info.quirks.name} records debug on access port {wanted}, but that "
            f"port is unreachable; falling back to AP{answered_on}, which can "
            "identify the target but may not be able to halt it. A closed access "
            "port on a TrustZone part is often the previously running firmware "
            "having locked it: try a full power cycle, not just a reset."
        )
        return answered_on

    @property
    def access_port(self) -> int:
        return 0 if self._access_port is None else self._access_port

    def read_memory(self, address: int, length_bytes: int) -> dict[int, int]:
        argv = self._connect_args(self.access_port) + [
            "-r32",
            f"0x{address:08X}",
            str(length_bytes),
        ]
        result = run_command(argv, timeout=90)
        words = parse_memory(result.stdout)
        if not words:
            raise ProbeError(
                f"No data read at 0x{address:08X}. Output was:\n{result.stdout.strip()}"
            )
        return words

    def core_registers(self) -> dict[str, int]:
        """Read the core registers.

        This HALTS the core. It is the most direct way to find out what a
        program is really doing, and it is also perturbing, so it is never used
        as part of a routine 'did it run' check.
        """
        argv = self._connect_args(self.access_port) + ["-coreReg"]
        result = run_command(argv, timeout=90)
        regs = parse_core_registers(result.stdout)
        if "PC" not in regs:
            raise ProbeError(
                "Could not read core registers. This usually means the core "
                "cannot be halted on this access port.\n"
                + result.stdout.strip()
            )
        return regs

    def program_flash(
        self, image: str, address: int, external_loader: str | None = None
    ) -> str:
        argv = self._connect_args(self.access_port)
        if external_loader:
            argv += ["-el", str(self.toolchain.external_loader(external_loader))]
        argv += ["-w", image, f"0x{address:08X}"]
        result = run_command(argv, timeout=300)
        if "File download complete" not in result.stdout:
            raise ProbeError(f"Programming failed:\n{result.stdout.strip()}")
        return result.stdout
