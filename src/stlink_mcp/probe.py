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
        "The probe may be held by ANOTHER PROCESS: an IDE, or a GDB server left "
        "by a different session or an earlier run. This server only knows about "
        "sessions it started itself, so it cannot see those and will report the "
        "target as unreachable instead. Check by PORT, not by process name: "
        "`ss -ltnp | grep 6123` and widen the range as needed; each server's "
        "command line carries `-i <probe serial>`. Do not use "
        "`pgrep ST-LINK_gdbserver`, which matches nothing at all, because Linux "
        "truncates process names to fifteen characters. That exact mistake has "
        "now invalidated two attempts to measure this behaviour.",
        "The target may be unpowered, held in reset, or the probe unplugged "
        "since it was last enumerated. list_probes reports access_ports for each "
        "probe, and 0 there means the probe answered but reached no target.",
    ]
    return "Could not reach the core. Most likely causes:\n  - " + "\n  - ".join(hints)


class Probe:
    """A thin, verified wrapper over STM32_Programmer_CLI."""

    def __init__(
        self,
        toolchain: Toolchain,
        access_port: int | None = None,
        serial: str | None = None,
    ) -> None:
        self.toolchain = toolchain
        self._access_port = access_port
        self.serial = serial
        """Which probe to talk to. Required once more than one is attached, since
        the tools silently pick the first otherwise, and "the first" is not
        stable across replugs."""
        self.connect_mode: str = "HOTPLUG"
        self.target_was_reset: bool = False
        """True when identification needed a resetting connect, which restarts
        whatever was running. Reported rather than hidden, because for anything
        inspecting a live program that is a destructive act."""
        self.degraded_reason: str | None = None
        """Set when the access port in use is not the one the family wants.

        Kept as state rather than raised, because identification still works in
        that situation and the caller should get the identity plus the warning,
        not an exception instead of both.
        """

    def _connect_args(self, access_port: int, mode: str = "HOTPLUG") -> list[str]:
        argv = [
            str(self.toolchain.programmer),
            "-c",
            "port=SWD",
            f"mode={mode}",
            f"ap={access_port}",
        ]
        if self.serial:
            argv.append(f"sn={self.serial}")
        return argv

    def identify(self, allow_reset: bool = True) -> TargetInfo:
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

        # HOTPLUG first: it does not disturb a running program, so state can be
        # inspected. HWRSTPULSE second: it pulses the reset line, which RECOVERS
        # an access port that running firmware has closed, at the cost of
        # restarting the target.
        modes = ["HOTPLUG", "HWRSTPULSE"] if allow_reset else ["HOTPLUG"]

        for mode in modes:
            for port in ports:
                result = run_command(self._connect_args(port, mode), timeout=90)
                info = parse_target_info(result.stdout)
                if info.connected:
                    self.connect_mode = mode
                    self.target_was_reset = mode != "HOTPLUG"
                    # allow_reset is forwarded, not defaulted. Without this a
                    # caller who asked not to disturb a running program still
                    # got one: the scan honoured the flag and the settle step
                    # then appended a resetting connect of its own.
                    self._access_port = self._settle_access_port(
                        info, port, mode, allow_reset=allow_reset
                    )
                    return info
                last = info

        return last

    def _settle_access_port(self, info: TargetInfo, answered_on: int,
                            mode: str = "HOTPLUG", allow_reset: bool = True) -> int:
        """Prefer the family's recorded access port over the one that answered.

        Two things make this more than a preference. Identity is readable on a
        port that cannot halt the core, so answering is not the same as being
        usable. And on parts where running firmware closes the debug port, the
        wanted port only comes back on a RESETTING connect: HOTPLUG will keep
        failing on it forever while AP0 answers happily.

        So the wanted port is retried with a recovering mode before giving up,
        and giving up is recorded as a degradation rather than hidden.
        """
        wanted = info.quirks.access_port
        if wanted == answered_on:
            return answered_on

        attempts = [mode]
        if allow_reset and "HWRSTPULSE" not in attempts:
            attempts.append("HWRSTPULSE")

        for attempt in attempts:
            result = run_command(self._connect_args(wanted, attempt), timeout=90)
            if parse_target_info(result.stdout).connected:
                self.connect_mode = attempt
                self.target_was_reset = attempt != "HOTPLUG"
                return wanted

        self.degraded_reason = (
            f"{info.quirks.name} records debug on access port {wanted}, but it is "
            f"unreachable even with a resetting connect; falling back to "
            f"AP{answered_on}, which can identify the target but may not be able "
            "to halt it."
        )
        return answered_on

    def gdb_access_port(self, info: TargetInfo) -> int:
        """Which access port the GDB server should be told to use.

        Deliberately taken from the family table, not from whichever port
        answered a programmer connect. Those are different questions:
        STM32_Programmer_CLI cannot reach AP1 on a board that is actively
        running firmware, but the GDB server attaches to it perfectly well. Using
        the programmer's answer as a gate refuses runs that would have worked.
        """
        return info.quirks.access_port

    @property
    def access_port(self) -> int:
        return 0 if self._access_port is None else self._access_port

    def read_memory(self, address: int, length_bytes: int) -> dict[int, int]:
        argv = self._connect_args(self.access_port, self.connect_mode) + [
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
        argv = self._connect_args(self.access_port, self.connect_mode) + ["-coreReg"]
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
        argv = self._connect_args(self.access_port, self.connect_mode)
        if external_loader:
            argv += ["-el", str(self.toolchain.external_loader(external_loader))]
        argv += ["-w", image, f"0x{address:08X}"]
        result = run_command(argv, timeout=300)
        if "File download complete" not in result.stdout:
            raise ProbeError(f"Programming failed:\n{result.stdout.strip()}")
        return result.stdout


# ---------------------------------------------------------------------------
# Probe enumeration
#
# `STM32_Programmer_CLI -l` prints the same serial number in two unrelated
# sections: a "Connected ST-LINK Probes List" with firmware and access-port
# count, and a "UART Interface" listing of every serial port on the machine,
# where each probe's virtual COM port is annotated with its serial. A naive
# scan for "ST-LINK SN" therefore reports twice as many probes as exist. The
# first version of this parser did exactly that and claimed 4 probes for 2
# boards.
#
# The UART section is worth parsing rather than skipping, because it is the only
# place that says WHICH /dev/ttyACM* belongs to which board. With two boards
# attached, picking the first ttyACM* would verify a run against the wrong
# board's console around half the time, and that failure would look like the
# firmware working.
# ---------------------------------------------------------------------------

_SECTION = re.compile(r"^=+\s*(.+?)\s*=+\s*$", re.M)
_PROBE_ENTRY = re.compile(r"^ST-Link Probe\s+\d+\s*:", re.M)


@dataclass(frozen=True)
class ProbeInfo:
    serial: str
    firmware: str | None = None
    board: str | None = None
    console_port: str | None = None
    access_ports: int | None = None
    """How many debug access ports the probe saw, from `Access Port Number`.

    Zero means the probe enumerated but could not reach the target, which is
    what a board whose running firmware has closed the debug port looks like.
    Worth keeping because it separates "no board" from "board that will not
    answer", which the connect error message alone does not.
    """


def _field(block: str, label: str) -> str | None:
    """Value of a `Label : value` line, or None when the field is blank.

    The separator is deliberately not `\\s*`. That matches newlines, so a blank
    field captures the first token of the line below it: with the debug port
    closed the probe prints an empty `ST-LINK FW`, and every such board was
    reported as running firmware "Access", taken from `Access Port Number` on
    the next line. The last probe in a list took the section's closing rule of
    dashes as its board name the same way.
    """
    match = re.search(
        rf"^[^\S\n]*{re.escape(label)}[^\S\n]*:[^\S\n]*(.*?)[^\S\n]*$",
        block,
        re.M,
    )
    if match is None:
        return None
    return match.group(1) or None


def _section(output: str, name_fragment: str) -> str:
    """The body of a '===== X =====' section, up to the next section header."""
    for match in _SECTION.finditer(output):
        if name_fragment.lower() in match.group(1).lower():
            nxt = _SECTION.search(output, match.end())
            return output[match.end():nxt.start() if nxt else len(output)]
    return ""


def _console_ports(output: str) -> dict[str, str]:
    """Map probe serial to serial-port path, from the UART section."""
    uart = _section(output, "UART")
    mapping: dict[str, str] = {}
    # Each virtual COM entry reads: ST-LINK SN: <sn> / Port: .. / Location: <path>
    # Same-line separators for the same reason as _field: a blank serial must
    # fail to match and be skipped, never absorb the next line and map a real
    # console path onto a serial that is not there.
    for match in re.finditer(
        r"ST-LINK SN:[^\S\n]*(\S+).*?^Location:[^\S\n]*(\S+)", uart, re.M | re.S
    ):
        mapping[match.group(1)] = match.group(2)
    return mapping


def parse_probe_list(output: str) -> list[ProbeInfo]:
    """One entry per physically attached probe."""
    stlink = _section(output, "STLink")
    consoles = _console_ports(output)

    entries: list[ProbeInfo] = []
    positions = [m.start() for m in _PROBE_ENTRY.finditer(stlink)]

    for index, begin in enumerate(positions):
        end = positions[index + 1] if index + 1 < len(positions) else len(stlink)
        block = stlink[begin:end]

        serial = _field(block, "ST-LINK SN")
        if not serial:
            continue

        ports = _field(block, "Access Port Number")

        entries.append(ProbeInfo(
            serial=serial,
            firmware=_field(block, "ST-LINK FW"),
            board=_field(block, "Board Name"),
            console_port=consoles.get(serial),
            access_ports=int(ports) if ports and ports.isdigit() else None,
        ))

    return entries


def list_probes(toolchain: Toolchain) -> list[ProbeInfo]:
    result = run_command([str(toolchain.programmer), "-l"], timeout=90)
    return parse_probe_list(result.stdout)
