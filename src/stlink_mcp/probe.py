"""Talking to the target through STM32_Programmer_CLI.

Everything here parses the CLI's human-readable output, which is the only
interface it offers. The parsers are deliberately strict: if the expected field
is absent we say so, rather than defaulting and reporting a confident wrong
answer.
"""

from __future__ import annotations

import dataclasses

import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .quirks import FamilyQuirks, for_device_id
from .toolchain import Toolchain, run_command, strip_ansi


class ProbeError(RuntimeError):
    pass


#: The debug interfaces a probe can be asked for. SWD is the default because
#: every probe that does JTAG also does SWD, and the on-board STLINK-V3E of a
#: NUCLEO-H743ZI2 does only SWD (measured 2026-10-01).
SWD = "swd"
JTAG = "jtag"
INTERFACES = (SWD, JTAG)


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
    interface: str | None = None
    """The interface the connect banner says it used, "swd" or "jtag": the
    observation, not the request. None when the banner did not say."""
    interface_khz: int | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def quirks(self) -> FamilyQuirks:
        return for_device_id(self.device_id)


_BANNER_SERIAL = re.compile(r"^ST-LINK SN\s*:\s*(\S+)", re.M)

# "SWD freq    : 8000 KHz" or "JTAG freq   : 9000 KHz", captured from both.
_INTERFACE_CLOCK = re.compile(r"^(SWD|JTAG) freq\s*:\s*(\d+)\s*KHz", re.M)

_FIELDS = {
    "stlink_serial": re.compile(r"^ST-LINK SN\s*:\s*(\S+)", re.M),
    "stlink_firmware": re.compile(r"^ST-LINK FW\s*:\s*(\S+)", re.M),
    "board": re.compile(r"^Board\s*:\s*(.+?)\s*$", re.M),
    "device_name": re.compile(r"^Device name\s*:\s*(.+?)\s*$", re.M),
    "cpu": re.compile(r"^Device CPU\s*:\s*(.+?)\s*$", re.M),
    "revision": re.compile(r"^Revision ID\s*:\s*(.+?)\s*$", re.M),
}
_DEVICE_ID = re.compile(r"^Device ID\s*:\s*0x([0-9A-Fa-f]+)", re.M)
_ERROR = re.compile(r"^(?:Error:\s*(.+?)|(ST-LINK error \(.+?\)))\s*$", re.M)

#: The port ST's stlink-server listens on. GDB servers started with -t start it.
STLINK_SERVER_PORT = 7184


def sharing_daemon_running(proc_net: tuple[str, ...] = ("/proc/net/tcp", "/proc/net/tcp6")) -> bool:
    """Whether ST's stlink-server is listening, so every connect must be shared.

    While it runs it may keep a probe open that nothing holds: after a shared
    read of another board, a plain connect to an STM32F407 whose GDB server had
    been released failed with DEV_CONNECT_ERR until the daemon exited, while a
    shared one worked (2026-10-04). With no daemon a shared connect starts one
    and took 30 s, against 0.1 s plain, so shared is used only while it runs.

    Read from the kernel's table of listening sockets where there is one, so
    the check neither connects to the daemon nor binds its port; elsewhere, a
    bind test, as runner.port_is_taken does."""
    tables = [Path(t) for t in proc_net if Path(t).exists()]
    if tables:
        for table in tables:
            try:
                rows = table.read_text().splitlines()[1:]
            except OSError:
                continue
            for row in rows:
                cols = row.split()
                if len(cols) > 3 and cols[3] == "0A" and cols[1].endswith(f":{STLINK_SERVER_PORT:04X}"):
                    return True
        return False
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("", STLINK_SERVER_PORT))
        except OSError:
            return True
        return False

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

    # A probe with no board of its own, such as an ST-LINK/V2, prints
    # "Board       : --" (captured 2026-10-01); list_probes reports None for it.
    if info.board in ("--", ""):
        info.board = None

    clock = _INTERFACE_CLOCK.search(output)
    if clock:
        info.interface = clock.group(1).lower()
        info.interface_khz = int(clock.group(2))

    dev = _DEVICE_ID.search(output)
    if dev:
        info.device_id = int(dev.group(1), 16)

    # Deduplicate: the CLI retries the connection and repeats every error.
    seen: list[str] = []
    for prefixed, bare in _ERROR.findall(output):
        err = prefixed or bare
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


#: Read last in every programmer call that connects or reads: the core's CPUID
#: register, whose top byte is 0x41 (Arm) on every Cortex-M. On an STM32H743
#: whose core slept on WFI, every address read 0x01010001 with no error, the
#: device ID too, and read_memory reported it verified (2026-10-03).
CPUID = 0xE000ED00
_CANARY = ["-r32", f"0x{CPUID:08X}", "4"]


def canary_problem(output: str) -> str | None:
    """Why nothing in this call's output can be trusted, or None. A canary read
    that failed outright is not a problem here: the caller's own reads say so."""
    value = parse_memory(output).get(CPUID)
    if value is None or value >> 24 == 0x41:
        return None
    return (
        f"The debug connection returned 0x{value:08X} for the core's CPUID register "
        "(0xE000ED00), which begins 0x41 on every Cortex-M part, so nothing read in "
        "the same call can be trusted. On an STM32H743 whose core slept on WFI, every "
        "address read 0x01010001 this way, with no error, until a connect under reset "
        "(2026-10-03); board_state and reset_target end with one."
    )


DHCSR, DEMCR, DWT_PCSR, DFSR = 0xE000EDF0, 0xE000EDFC, 0xE000101C, 0xE000ED30


@dataclass(frozen=True)
class CoreState:
    """What DHCSR, DEMCR, DWT_PCSR and DFSR said, read without halting the core."""
    dhcsr: int
    demcr: int | None
    pc_samples: tuple[int, ...]
    dfsr: int | None = None
    """Sticky debug events. VCATCH, bit 3, is set when a vector catch halted the
    core: a held GDB server resets and halts a core that locked up that way."""

    @property
    def vector_catch(self) -> bool:
        return self.dfsr is not None and bool(self.dfsr & (1 << 3))

    @property
    def state(self) -> str:
        # Measured on 2026-10-03: running cores on the H7, the F407 and the N6
        # read S_RETIRE_ST set (an instruction retired since DHCSR was last read);
        # halted ones S_HALT, the F407 and the N6; on the N6 a fault inside the
        # fault handler read S_LOCKUP. The H7 after that same fault, and after
        # reset_target with its own firmware, read neither bit and S_RETIRE_ST
        # clear, the two alike. A sleeping core could not be read on any of them.
        if self.dhcsr & (1 << 19):
            return "locked up"
        if self.dhcsr & (1 << 17):
            return "halted"
        if self.dhcsr & (1 << 18):
            return "sleeping"
        if self.dhcsr & (1 << 24):
            return "running"
        return "not seen running"

    @property
    def sampler_on(self) -> bool:
        """DWT_PCSR samples only while DEMCR.TRCENA is set, which a GDB session
        does and a reset undoes: after a reset it read 0 on the H7."""
        return self.demcr is None or bool(self.demcr & (1 << 24))


def parse_reads_in_order(output: str) -> list[tuple[int, int]]:
    """Each -r32 of one word, in the order printed: the same address read
    several times gives several entries, which parse_memory would merge."""
    return [(int(m.group(1), 16), int(m.group(2).split()[0], 16)) for m in _MEM_LINE.finditer(output)]


def _trusted(info: TargetInfo, output: str) -> TargetInfo:
    """A connect whose canary read is wrong is not a connect: its device ID came
    from the same untrustworthy reads."""
    problem = canary_problem(output) if info.connected else None
    return dataclasses.replace(info, connected=False, errors=[*info.errors, problem]) if problem else info


def _without_canary(words: dict[int, int], ranges: list[tuple[int, int]]) -> dict[int, int]:
    if any(start <= CPUID < start + length for start, length in ranges):
        return words
    return {address: word for address, word in words.items() if address != CPUID}


def parse_core_registers(output: str) -> dict[str, int]:
    """Parse '-coreReg' output into {register: value}."""
    return {name: int(value, 16) for name, value in _CORE_REG.findall(output)}


#: Where internal flash sits on most, but NOT all, STM32 parts.
#:
#: An earlier version of this comment said 94 of the 104 device databases put it
#: here "and none of them puts it anywhere else". That was false, and it was the
#: third count published in one day that measured its author's filter rather
#: than the data. Measured properly, with `tools/survey_device_db.py`'s parser:
#:
#:   94 of 104 databases carry an Embedded Flash base at 0x0800xxxx
#:   10 do not. Of those, FIVE have internal flash at 0x10040000 - the
#:      STM32WB05/07/09 and STM32WL3/WL3R - and five have no internal flash at
#:      all: the STM32N6 and the four STM32MP entries.
#:
#: Individual databases also declare the same flash at a 0x0C00xxxx TrustZone
#: secure alias and, on some H7/F7 entries, at a 0x0020xxxx bank mapping. So
#: this constant is one common case, not a universal fact.
INTERNAL_FLASH_BASE = 0x08000000


def reset_vector_address(quirks: FamilyQuirks) -> int | None:
    """Where to read the reset vector, to predict the PC after a reset.

    Only a part that boots out of its own internal flash has a post-reset PC
    this table can predict. On a part whose boot ROM loads a signed image from
    external memory, the STM32N6 being the one here, the core comes up inside
    that ROM at an address nothing records, and whatever was running in RAM is
    gone. None is the honest answer, and it makes the caller report a reset it
    could not check rather than compare the PC against a number that does not
    describe it.

    KNOWN NARROW, and narrow in a way that costs coverage rather than
    correctness. The test is equality with INTERNAL_FLASH_BASE, so a
    STM32WB05/07/09 or STM32WL3 with a correctly measured flash_base of
    0x10040000 executes from its own internal flash exactly like an H7, has an
    equally predictable post-reset PC, and is refused anyway. That is a missed
    verification, never a wrong one: the caller gets `unverified` with a stated
    reason. Widening it needs one of those parts on a bench, which is the same
    bar every other entry in this project had to clear.
    """
    return INTERNAL_FLASH_BASE if quirks.flash_base == INTERNAL_FLASH_BASE else None


def reset_landed_at_vector(pc: int | None, reset_handler: int | None) -> bool:
    """Whether a halted core is sitting on the reset handler.

    The vector table stores the handler address with bit 0 set, which marks
    Thumb state, and the PC reads back with that bit clear. Comparing the two
    without masking is false for every correct reset, which is worse than having
    no check at all: it would report a reset that did happen as one that did
    not, and send someone to debug working hardware.
    """
    if pc is None or reset_handler is None:
        return False
    return pc == (reset_handler & ~1)


_SWV_STARTED = ">>>>>> Reception Started <<<<<<"
_SWV_ENDED = ">>>>>> Reception Ended <<<<<<"


def parse_swv_log(log: str) -> str | None:
    """What arrived, from the log `-startswv` writes; None if reception never began.

    The programmer writes a header, a tab and the start marker, a blank line,
    then what arrived, then a newline of its own, a tab and the end marker
    (captured 2026-10-02). Characters that did not decode are already U+FFFD
    in the file, so the bytes behind them are not recoverable."""
    start = log.find(_SWV_STARTED)
    if start < 0:
        return None
    body = log[start + len(_SWV_STARTED):].removeprefix("\n\n")
    end = body.find(_SWV_ENDED)
    if end >= 0:
        body = body[:end].removesuffix("\t").removesuffix("\n")
    return body


def no_data_read(address: int, output: str) -> ProbeError:
    """A read that connected and identified the target, and then returned nothing.

    The programmer's line for it is the same for two different causes, measured
    on 2026-10-02: a read across the end of flash or RAM on an STM32F407 (and
    on an STM32H7 the day before) fails whole, and so does any read on an
    STM32N6 whose image has turned the SYSCFG clock off and closed AP1, though
    the part still identifies on that port."""
    message = f"No data read at 0x{address:08X}."
    if "failed to read the requested memory content" in output:
        message += (
            " The programmer connected and identified the target, and the read "
            "itself failed. Two things fail this way: a range that runs past the "
            "end of flash or RAM, which fails whole rather than returning the part "
            "that exists; and a debug access port that has closed although the part "
            "still identifies, as on an STM32N6 once its image's start-up code has "
            "turned the SYSCFG clock off, where only a power cycle reopens it."
        )
    return ProbeError(f"{message} Output was:\n{output.strip()}")


def explain_connect_failure(info: TargetInfo, interface: str = SWD) -> str:
    """Turn an unhelpful CLI error into the two things it usually means.

    This exists because 'Unable to get core ID' is emitted for both a wrong
    access port and a boot configuration that closes the debug port, and those
    have completely different fixes.
    """
    if info.connected:
        return ""
    canary = next((e for e in info.errors if "CPUID register" in e), None)
    if canary:
        return canary
    if any("DEV_CONNECT_ERR" in e for e in info.errors):
        return (
            "The programmer could not open this probe: another program has it open. With "
            "ST's stlink-server running this server connects in its shared mode, so it is "
            "a program outside it, such as OpenOCD, pyOCD or an IDE debugging without "
            "sharing. Close that session, then try again."
        )
    if any("Old ST-LINK firmware" in e for e in info.errors):
        return (
            "The probe's firmware is too old for this STM32CubeProgrammer, which "
            "refuses to use it. STLinkUpgrade, in the STM32CubeCLT install, updates "
            "it: select the probe, Open in update mode, Upgrade."
        )
    if any("Serial number not found" in e for e in info.errors):
        return (
            "The programmer could not select a probe with that serial. Either none "
            "is attached, or its firmware is too old for this programmer: an "
            "ST-LINK/V2 on V2J16S4 was listed by list_probes and still could not be "
            "selected until it was upgraded (2026-10-01). list_probes shows the "
            "attached probes with their firmware; STLinkUpgrade, in the STM32CubeCLT "
            "install, updates an old one."
        )
    # Each line is a cause and what can be done about it; the history behind
    # them is in docs/findings.md. The texts used to tell an agent to run shell
    # commands and recount failed measurements (agent-use evaluation, 2026-10-04).
    hints = []
    if interface == JTAG:
        hints.append(
            "The probe may not do JTAG, or not be wired for it: a NUCLEO's on-board "
            "STLINK-V3E answers 'Unable to get core ID' over JTAG while SWD works. "
            "Try interface 'swd'."
        )
    hints += [
        "The running program may have closed the debug port: taken the SWD pins, "
        "stopped a clock or slept without debug. A tool that may reset gets past it "
        "with a connect under reset, which restarts the program.",
        "The boot configuration may keep the debug port closed: parts that boot "
        "from external memory open it only in the development boot mode set by a "
        "BOOT pin or switch.",
        "Another program may have the probe open: an IDE, OpenOCD, or a GDB server "
        "this server did not start, which it cannot see.",
        "The target may be unpowered or held in reset: list_probes gives access_ports "
        "0 for a probe that answered and reached no target.",
    ]
    return "Could not reach the core. Most likely causes:\n  - " + "\n  - ".join(hints)


class Probe:
    """A thin, verified wrapper over STM32_Programmer_CLI."""

    def __init__(
        self,
        toolchain: Toolchain,
        access_port: int | None = None,
        serial: str | None = None,
        interface: str = SWD,
        try_first: int | None = None,
        shared: bool = False,
    ) -> None:
        if interface not in INTERFACES:
            raise ValueError(f"interface {interface!r} is not one of {', '.join(INTERFACES)}")
        self.interface = interface
        """SWD or JTAG, for every connect this probe makes. A parameter (N8):
        the wiring decides, and nothing here may assume one."""
        self._try_first = try_first
        """The access port to scan first, usually the one this probe settled on
        last time. Only the order changes: the same ports are scanned. On an
        STM32N6 it saves a connect per call, 1.1 s (measured 2026-10-01),
        because AP1 answers first instead of AP0 and then AP1."""
        self.shared = shared
        """Connect in ST's shared mode, beside a GDB server started with -t that
        holds this probe. Reads then reach the core without halting it."""
        self.toolchain = toolchain
        self._access_port = access_port
        self.serial = serial
        """Which probe to talk to. Required once more than one is attached, since
        the tools silently pick the first otherwise, and "the first" is not
        stable across replugs."""
        self.connect_mode: str = "HOTPLUG"
        self.target_was_reset: bool = False
        """True once a resetting connect has been ISSUED, which restarts
        whatever was running. Reported rather than hidden, because for anything
        inspecting a live program that is a destructive act.

        Issued, not succeeded. The pulse on nRST goes out before the debug port
        is asked to answer, which is what reset_target's "the pulse was issued
        and the port did not answer" branch rests on. This used to be set only
        when the resetting connect worked, so a board whose port stayed shut was
        restarted and reported as untouched."""
        self.access_port_provenance: str | None = None
        """Where the access port in use came from, once identify() has run."""
        self.other_ports_that_answered: list[int] = []
        self.unmeasured_port_warning: str | None = None
        """Set when the port in use was never measured for this part. Distinct
        from degraded_reason, which means the family's recorded port could not
        be reached at all: this one means there is no recorded port to reach."""
        self.degraded_reason: str | None = None
        """Set when the access port in use is not the one the family wants.

        Kept as state rather than raised, because identification still works in
        that situation and the caller should get the identity plus the warning,
        not an exception instead of both.
        """

    def _run(self, argv: list[str], timeout: float = 90):
        """Run the programmer, and refuse an answer from a probe nobody asked for.

        STM32_Programmer_CLI ignores an `sn=` it cannot parse as hex and
        connects to the first probe it finds: sn=NONE and sn=X both answered
        from an ST-LINK/V2 on 2026-10-01, while sn=123 got "Serial number not
        found". So the banner's own serial is compared with the one asked for,
        on every call that names one."""
        result = run_command(argv, timeout=timeout)
        if self.serial:
            match = _BANNER_SERIAL.search(result.stdout)
            if match and match.group(1).upper() != self.serial.upper():
                raise ProbeError(
                    f"Asked for probe {self.serial}, and the programmer answered from "
                    f"probe {match.group(1)}. Nothing from that answer is used."
                )
        return result

    def _connect_args(self, access_port: int, mode: str = "HOTPLUG") -> list[str]:
        argv = [
            str(self.toolchain.programmer),
            "-c",
            f"port={self.interface.upper()}",
            f"mode={mode}",
            f"ap={access_port}",
        ]
        if self.serial:
            argv.append(f"sn={self.serial}")
        if self.shared or sharing_daemon_running():
            argv.append("shared")
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
        if self._access_port is not None:
            ports = [self._access_port]
        elif self._try_first in (0, 1):
            ports = [self._try_first, 1 - self._try_first]
        else:
            ports = [0, 1]
        last = TargetInfo(connected=False)

        # HOTPLUG first: it does not disturb a running program, so state can be
        # inspected. HWRSTPULSE second: it pulses the reset line and then
        # connects. UR last: it connects while holding the target in reset, so
        # no program runs first. On an STM32H743 whose program had made the SWD
        # pins inputs, HOTPLUG and HWRSTPULSE each failed after 30 s with
        # "Unable to get core ID" and UR reached the core in 0.1 s (2026-10-03);
        # on an STM32N6 UR fails even on a healthy board, so it stays last.
        # Never a resetting connect beside a held GDB server: a reset made that
        # way broke the GDB session on the H7 and the F407 (2026-10-03).
        allow_reset = allow_reset and not self.shared
        modes = ["HOTPLUG", "HWRSTPULSE", "UR"] if allow_reset else ["HOTPLUG"]

        for mode in modes:
            for port in ports:
                if mode != "HOTPLUG":
                    # Recorded before the connect, not after it succeeds: the
                    # pulse restarts the target whether or not the port answers.
                    self.target_was_reset = True
                result = self._run(self._connect_args(port, mode) + _CANARY, timeout=90)
                info = _trusted(parse_target_info(result.stdout), result.stdout)
                if info.connected:
                    self.connect_mode = mode
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
        self.access_port_provenance = info.quirks.provenance_of("access_port")

        if wanted == answered_on:
            # Agreeing with a number nobody measured is not agreement.
            #
            # For an uncharacterised part the family's port comes from DEFAULT,
            # which is 0 and is `assumed`. The port that answered is also 0,
            # because the scan tries 0 first and stops when it works. So
            # `wanted == answered_on` is satisfied by two independent guesses
            # matching, and the function used to return here reporting nothing.
            #
            # That is the STM32N6 trap for any part this project has not
            # characterised: AP0 answers device ID, name, CPU and revision
            # perfectly well while being unable to halt the core, so the first
            # symptom is "Failed to halt target" much later, which reads as
            # broken hardware.
            #
            # The core cannot be halted here to settle it, because halting is
            # perturbing and this method runs during plain identification. What
            # CAN be done without touching the running program is ask whether
            # any other port answers at all, and say plainly that the number in
            # use was never measured.
            if not info.quirks.is_measured("access_port"):
                self._note_unmeasured_port(answered_on)
            return answered_on

        attempts = [mode]
        if allow_reset:
            attempts += [m for m in ("HWRSTPULSE", "UR") if m not in attempts]

        for attempt in attempts:
            if attempt != "HOTPLUG":
                # As in identify(): a resetting connect that fails still reset
                # the target. On an STM32N6 whose image has closed AP1 this
                # branch pulses the board and then fails, and it used to report
                # target_was_reset False for a board it had just restarted.
                self.target_was_reset = True
            result = self._run(self._connect_args(wanted, attempt) + _CANARY, timeout=90)
            if _trusted(parse_target_info(result.stdout), result.stdout).connected:
                self.connect_mode = attempt
                # Settling here does not make an unmeasured port measured. This
                # path was reached only from a scan that answered elsewhere, and
                # once a probe's last port is tried first it is reached for an
                # assumed port too, where the warning used to be skipped.
                if not info.quirks.is_measured("access_port"):
                    self._note_unmeasured_port(wanted)
                return wanted

        self.degraded_reason = (
            f"{info.quirks.name} records debug on access port {wanted}, but it is "
            f"unreachable even with a resetting connect; falling back to "
            f"AP{answered_on}, which can identify the target but may not be able "
            "to halt it."
        )
        return answered_on

    def core_state(self, samples: int = 5) -> CoreState:
        """DHCSR and `samples` reads of DWT_PCSR in one programmer call, none of
        which halts the core. ProbeError when the reads cannot be trusted or did
        not happen, as when the core sleeps on WFI."""
        argv = self._connect_args(self.access_port, "HOTPLUG") + ["-r32", f"0x{DHCSR:08X}", "4"]
        argv += ["-r32", f"0x{DEMCR:08X}", "4"] + ["-r32", f"0x{DWT_PCSR:08X}", "4"] * samples
        argv += ["-r32", f"0x{DFSR:08X}", "4"]
        result = self._run(argv + _CANARY, timeout=90)
        problem = canary_problem(result.stdout)
        if problem:
            raise ProbeError(problem)
        reads = parse_reads_in_order(result.stdout)
        dhcsr = next((word for address, word in reads if address == DHCSR), None)
        demcr = next((word for address, word in reads if address == DEMCR), None)
        pcs = tuple(word for address, word in reads if address == DWT_PCSR)
        dfsr = next((word for address, word in reads if address == DFSR), None)
        if dhcsr is None or len(pcs) < samples:
            raise ProbeError(
                "The core's debug status could not be read without halting it: "
                f"{len(pcs)} of {samples} program-counter samples came back. A core asleep "
                "on WFI or WFE answered no reads on the STM32N6 and the STM32F407 "
                "(2026-10-03).\n" + result.stdout.strip()[-400:]
            )
        return CoreState(dhcsr, demcr, pcs, dfsr)

    def _note_unmeasured_port(self, answered_on: int) -> None:
        """Record that the access port in use is a guess, and what else answered.

        One extra hotplug connect per alternative port, and only for a part
        whose port was never measured, so a characterised family pays nothing.
        The connect does not reset or halt, so a running program is undisturbed.
        It is always HOTPLUG. It used to reuse the mode that identified the
        target, so a part reachable only by a resetting connect was pulsed a
        second time here, under a docstring promising it would not be.

        Another port answering is not proof that it is the right one, and this
        deliberately does not switch to it. It is the single most useful fact
        available for the failure it warns about, because on the one part known
        to need a non-zero port BOTH answer identity and only one can halt.
        """
        for port in (0, 1):
            if port == answered_on:
                continue
            result = self._run(self._connect_args(port, "HOTPLUG"), timeout=90)
            if parse_target_info(result.stdout).connected:
                self.other_ports_that_answered.append(port)

        others = ", ".join(f"AP{p}" for p in self.other_ports_that_answered)
        self.unmeasured_port_warning = (
            f"Using access port {answered_on}, which nobody measured for this "
            f"part: it is the {self.access_port_provenance} default. Identity is "
            "readable on a port that cannot halt the core, so this connect "
            "succeeding does not mean the port is right."
            + (
                f" {others} also answered, and on the one family known to need a "
                "non-zero port that is exactly what the wrong port looks like."
                if self.other_ports_that_answered
                else " No other access port answered."
            )
        )

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
        ] + _CANARY
        result = self._run(argv, timeout=90)
        problem = canary_problem(result.stdout)
        if problem:
            raise ProbeError(problem)
        words = _without_canary(parse_memory(result.stdout), [(address, length_bytes)])
        if not words:
            raise no_data_read(address, result.stdout)
        return words

    def read_ranges(self, ranges: list[tuple[int, int]]) -> dict[int, int]:
        """Several reads in one programmer call, which takes a -r32 per range and
        prints each (measured 2026-10-02, three ranges on an STM32H7 and an F407)."""
        argv = self._connect_args(self.access_port, self.connect_mode)
        for address, length_bytes in ranges:
            argv += ["-r32", f"0x{address:08X}", str(length_bytes)]
        result = self._run(argv + _CANARY, timeout=90)
        problem = canary_problem(result.stdout)
        if problem:
            raise ProbeError(problem)
        words = _without_canary(parse_memory(result.stdout), ranges)
        if not words:
            raise no_data_read(ranges[0][0], result.stdout)
        return words

    def identify_and_read(
        self, address: int, length_bytes: int
    ) -> tuple[TargetInfo, dict[int, int]] | None:
        """Connect and read in one programmer call, where identify() could add
        nothing; None when it could, and the caller then does both in turn.

        One call instead of two halves the time on an STM32N6, where every
        connect costs about 1.1 s (measured 2026-10-01). It is tried only on
        the port this probe settled on before, with a hotplug connect, and
        kept only when that port is the family's MEASURED port: then identify()
        would scan nothing else, settle nowhere else and warn of nothing. A
        read changes nothing on the target, so a discarded attempt costs only
        its time."""
        port = self._try_first
        if self._access_port is not None or port is None:
            return None
        argv = self._connect_args(port, "HOTPLUG") + ["-r32", f"0x{address:08X}", str(length_bytes)] + _CANARY
        result = self._run(argv, timeout=90)
        info = parse_target_info(result.stdout)
        if not info.connected:
            return None
        problem = canary_problem(result.stdout)
        if problem:
            raise ProbeError(problem)
        quirks = info.quirks
        if quirks.access_port != port or not quirks.is_measured("access_port"):
            return None
        self._access_port = port
        self.connect_mode = "HOTPLUG"
        self.access_port_provenance = quirks.provenance_of("access_port")
        words = parse_memory(result.stdout)
        if not words:
            raise no_data_read(address, result.stdout)
        return info, words

    def capture_swo(self, clock_hz: int, seconds: float, itm_port: int) -> str:
        """What the target writes to ITM stimulus port `itm_port`, over SWO, for
        `seconds` from the moment reception starts.

        The programmer's -startswv connects hotplug, sets up the trace unit and
        the pin itself, and runs until it reads "E" on its input; it then exits
        0 and its log is complete (measured on an STM32F407 and an STM32H7,
        2026-10-02). It takes the core clock in MHz and uses a fraction as
        given. The GDB server's own SWO port was tried first and is not used:
        it streams only while a GDB client stays attached, and on the H7 it
        never started receiving at all."""
        if self.interface != SWD:
            raise ProbeError(
                "SWO needs SWD: the trace output shares its pin with JTAG's TDO."
            )
        mhz = f"{clock_hz / 1_000_000:.6f}".rstrip("0").rstrip(".")
        with tempfile.TemporaryDirectory(prefix="stlink-mcp-swo-") as scratch:
            log = Path(scratch) / "swv.log"
            argv = self._connect_args(self.access_port, "HOTPLUG") + [
                "-startswv", f"freq={mhz}", f"portnumber={itm_port}", str(log),
            ]
            proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, errors="replace",
            )
            lines: list[str] = []
            started = threading.Event()

            def drain() -> None:
                for line in proc.stdout:
                    lines.append(line)
                    if "Reception Started" in line:
                        started.set()

            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
            try:
                deadline = time.monotonic() + 60
                while not started.is_set() and proc.poll() is None and time.monotonic() < deadline:
                    started.wait(0.1)
                output = strip_ansi("".join(lines))
                answered = _BANNER_SERIAL.search(output)
                if self.serial and answered and answered.group(1).upper() != self.serial.upper():
                    raise ProbeError(
                        f"Asked for probe {self.serial}, and the programmer answered from "
                        f"probe {answered.group(1)}. Nothing from that answer is used."
                    )
                if not started.is_set():
                    raise ProbeError("The programmer did not start SWO reception.\n" + output.strip())
                time.sleep(seconds)
            finally:
                # Stopped with its own E on every path, refusals included, so
                # the probe is not left receiving; killed only if that fails.
                if proc.poll() is None:
                    try:
                        proc.stdin.write("E\n")
                        proc.stdin.flush()
                        proc.wait(timeout=15)
                    except (OSError, subprocess.TimeoutExpired):
                        proc.kill()
                        proc.wait()
                reader.join(2)
            text = ""
            if log.is_file():
                with open(log, encoding="utf-8", errors="replace", newline="") as handle:
                    text = handle.read()
        body = parse_swv_log(text)
        if body is None:
            raise ProbeError(
                "The programmer's SWO log has no start of reception.\n"
                + strip_ansi("".join(lines)).strip()
            )
        return body

    def core_registers(self) -> dict[str, int]:
        """Read the core registers.

        This HALTS the core. It is the most direct way to find out what a
        program is really doing, and it is also perturbing, so it is never used
        as part of a routine 'did it run' check.
        """
        argv = self._connect_args(self.access_port, self.connect_mode) + ["-coreReg"]
        result = self._run(argv, timeout=90)
        regs = parse_core_registers(result.stdout)
        if "PC" not in regs:
            raise ProbeError(
                "Could not read core registers. This usually means the core "
                "cannot be halted on this access port.\n"
                + result.stdout.strip()
            )
        return regs

    def reset_and_halt(self, access_port: int) -> tuple[int | None, str]:
        """Reset the target and halt it, returning the program counter.

        THE RESET HAS TO BE THE CONNECT, not a command issued after one.
        Measured on a NUCLEO-H743ZI2 2026-08-16, whose reset handler is at
        0x0807FA98:

            -c mode=HOTPLUG -rst -halt -coreReg   PC = 0x0807FC2C
            -c mode=HOTPLUG -halt -rst -coreReg   PC = 0x0807FC3A
            -c mode=HWRSTPULSE -halt -coreReg     PC = 0x0807FA98
            -c mode=UR -coreReg                   PC = 0x0807FA98

        The first two reset the part perfectly well and then lose the race to
        halt it, so the core has already run several hundred bytes into
        start-up. They are useless for VERIFYING a reset, because the PC they
        report is not predictable and so cannot be compared against anything.
        Only a resetting connect mode lands on the reset vector exactly.

        UR is not used here even though it also works: it fails outright on the
        STM32N6, on a healthy board, so HWRSTPULSE is the one that generalises.
        """
        argv = self._connect_args(access_port, "HWRSTPULSE") + ["-halt", "-coreReg"]
        result = self._run(argv, timeout=90)
        regs = parse_core_registers(result.stdout)
        return regs.get("PC"), result.stdout

    def release_core(self, access_port: int) -> str:
        """Let a halted core run again, and check that it did.

        This is a SECOND connect, separate from the one that halted the core, so
        everything that can go wrong between two invocations applies: another
        process taking the probe, the board losing power, a USB glitch.

        The check is not decoration. `run_command` does not raise on a nonzero
        exit or on an error banner, only on a timeout or a missing binary, so an
        unchecked version returns cleanly while the core is still halted, and
        the caller then reports a running target that is stopped. Every other
        method here checks its own effect - `program_flash` requires "File
        download complete", `core_registers` requires a PC - and this one was the
        exception until a red-team pass pointed at it.

        "Core run" is what the CLI prints on success, observed on a NUCLEO-H743ZI2
        on 2026-08-16 immediately after a HWRSTPULSE connect and `-halt`.
        """
        argv = self._connect_args(access_port, "HOTPLUG") + ["-run"]
        result = self._run(argv, timeout=90)
        if "Core run" not in result.stdout:
            raise ProbeError(
                "Asked the core to resume and the programmer did not confirm it. "
                "The target is most likely still HALTED.\n" + result.stdout.strip()
            )
        return result.stdout

    def program_flash(
        self, image: str, address: int, external_loader: str | None = None
    ) -> str:
        argv = self._connect_args(self.access_port, self.connect_mode)
        if external_loader:
            argv += ["-el", str(self.toolchain.external_loader(external_loader))]
        argv += ["-w", image, f"0x{address:08X}"]
        result = self._run(argv, timeout=300)
        if "File download complete" not in result.stdout:
            message = f"Programming failed:\n{result.stdout.strip()}"
            if "failed to erase memory" in result.stdout:
                # The programmer stops at the erase and never says why. A
                # write-protected sector on an STM32F407 failed exactly this
                # way on 2026-10-02.
                message = (
                    "The programmer could not erase the flash it was to write. "
                    "Write protection on that sector, set in the option bytes, "
                    "fails this way; `STM32_Programmer_CLI -c port=SWD -ob displ` "
                    "shows it, and this tool does not change option bytes.\n"
                    + message
                )
            raise ProbeError(message)
        return result.stdout

    def read_back(
        self, address: int, size: int, external_loader: str | None = None
    ) -> bytes:
        """Read `size` bytes at `address` into memory, with the programmer's upload.

        The bytes are the evidence, not the programmer's banner: what comes back
        is compared with the image by the caller, so a write the programmer
        reported complete is checked against what flash actually holds. Uses a
        hotplug connect in the mode identify() settled on, so it neither resets
        nor halts anything identify() did not.
        """
        with tempfile.TemporaryDirectory(prefix="stlink-mcp-") as scratch:
            out = Path(scratch) / "readback.bin"
            argv = self._connect_args(self.access_port, self.connect_mode)
            if external_loader:
                argv += ["-el", str(self.toolchain.external_loader(external_loader))]
            argv += ["-u", f"0x{address:08X}", str(size), str(out)]
            result = self._run(argv, timeout=300)
            data = out.read_bytes() if out.is_file() else b""
        if len(data) != size:
            raise ProbeError(
                f"Reading {size} bytes back from 0x{address:08X} returned "
                f"{len(data)}. Output was:\n{result.stdout.strip()[-1500:]}"
            )
        return data


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
    console_source: str | None = None
    """Where console_port came from: "probe list" for the programmer's own
    UART mapping, or the setting that overrode it. None with no console."""
    console_note: str | None = None
    """Why a console setting was not applied to this probe, when it was not."""


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
        # On Windows the Location is the Win32 device path, \\.\COM3, beside
        # "Port: COM3" (captured on Windows 11, 2026-10-02). It is reported as COM3,
        # the name a user and pyserial's port list give it; pyserial opens both.
        mapping[match.group(1)] = match.group(2).removeprefix("\\\\.\\")
    return mapping


#: Values the vendor CLI prints where a serial should be, when it could not read
#: the probe at all.
_PLACEHOLDER_SERIALS = frozenset({"", "-", "--", "n/a", "none", "null"})


def _looks_like_a_probe_serial(value: str) -> bool:
    """Whether this is a serial rather than the CLI failing to read one.

    Real probes report a long alphanumeric identifier, 24 hex digits on every
    ST-LINK seen here. That is V3 generation only: an earlier version of this
    docstring claimed V2-1 as well, and no V2-1 probe has ever been attached to
    the machine this was written on, so the claim was inherited from belief
    rather than measured. The test below is therefore deliberately looser than
    "24 hex digits" - a length floor plus an alphanumeric check - because the
    older generations are the untested case and a rule tuned to V3 is the wrong
    thing to enforce on them.

    When a probe is present but unreadable, which happens on
    `DEV_USB_COMM_ERR`, the CLI still prints a numbered entry for it and fills
    the fields with a placeholder dash, or with uninitialised memory.

    Captured 2026-08-16 from two probes in that state, in consecutive runs:

        ST-LINK SN  : -              ST-LINK SN  : EAT
        Access Port Number  : -      Access Port Number  : 1214198062

    and a run earlier gave `-0_1_3` with 32512. The garbage differs each time,
    so it cannot be matched on. A length floor and an alphanumeric test separate
    both shapes from a real serial without assuming the serial is hex, since not
    every ST-LINK generation is guaranteed to be.
    """
    if value.casefold() in _PLACEHOLDER_SERIALS:
        return False
    return len(value) >= 8 and value.isalnum()


def count_unreadable_entries(output: str) -> int:
    """Probe entries the CLI printed but could not fill in.

    Reported rather than silently dropped: a board that is attached and not
    readable is a different situation from one that is absent, and it usually
    means a USB problem rather than anything about the target.
    """
    stlink = _section(output, "STLink")
    positions = [m.start() for m in _PROBE_ENTRY.finditer(stlink)]
    unreadable = 0
    for index, begin in enumerate(positions):
        end = positions[index + 1] if index + 1 < len(positions) else len(stlink)
        serial = _field(stlink[begin:end], "ST-LINK SN")
        if serial is None or not _looks_like_a_probe_serial(serial):
            unreadable += 1
    return unreadable


def parse_probe_list(output: str) -> list[ProbeInfo]:
    """One entry per physically attached and READABLE probe."""
    stlink = _section(output, "STLink")
    consoles = _console_ports(output)

    entries: list[ProbeInfo] = []
    positions = [m.start() for m in _PROBE_ENTRY.finditer(stlink)]

    for index, begin in enumerate(positions):
        end = positions[index + 1] if index + 1 < len(positions) else len(stlink)
        block = stlink[begin:end]

        serial = _field(block, "ST-LINK SN")
        if not serial or not _looks_like_a_probe_serial(serial):
            # An entry the CLI could not fill in. Counted by
            # count_unreadable_entries rather than presented as a probe: passing
            # "-" or "EAT" on as a serial would put it in the "pass one of these"
            # list and let a caller try to select it.
            continue

        ports = _field(block, "Access Port Number")

        entries.append(ProbeInfo(
            serial=serial,
            firmware=_field(block, "ST-LINK FW"),
            board=_field(block, "Board Name"),
            console_port=consoles.get(serial),
            access_ports=int(ports) if ports and ports.isdigit() else None,
            console_source="probe list" if serial in consoles else None,
        ))

    return entries


def _list_argv(toolchain: Toolchain) -> list[str]:
    """`-l`, and `shared` while the daemon runs: a plain listing showed a probe
    the daemon keeps with no firmware and no target (2026-10-04)."""
    return [str(toolchain.programmer), "-l"] + (["shared"] if sharing_daemon_running() else [])


def list_probes(toolchain: Toolchain) -> list[ProbeInfo]:
    result = run_command(_list_argv(toolchain), timeout=90)
    return parse_probe_list(result.stdout)


def list_probes_with_unreadable(
    toolchain: Toolchain,
) -> tuple[list[ProbeInfo], int, list[str]]:
    """Readable probes, how many entries could not be read at all, and the error
    lines the programmer printed in its ST-LINK section."""
    result = run_command(_list_argv(toolchain), timeout=90)
    return (
        parse_probe_list(result.stdout),
        count_unreadable_entries(result.stdout),
        listing_errors(result.stdout),
    )


_LISTING_ERROR = re.compile(r"^(?:Error:\s*(.+?)|(ST-LINK error \(.+?\)))\s*$", re.M)


def listing_errors(output: str) -> list[str]:
    """The error lines of the ST-LINK section, each once, in order.

    A listing with errors and no readable entry cannot say that nothing is
    attached: a probe the user cannot open (udev rules, a USB fault) prints an
    error and no entry. Kept so the caller can tell that from an empty bus
    (red-team RT10, 2026-09-03)."""
    seen: list[str] = []
    for match in _LISTING_ERROR.finditer(_section(output, "STLink")):
        line = (match.group(1) or match.group(2)).strip()
        if line not in seen:
            seen.append(line)
    return seen
