"""Talking to the target through STM32_Programmer_CLI.

Everything here parses the CLI's human-readable output, which is the only
interface it offers. The parsers are deliberately strict: if the expected field
is absent we say so, rather than defaulting and reporting a confident wrong
answer.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .quirks import FamilyQuirks, for_device_id
from .toolchain import Toolchain, run_command


class ProbeError(RuntimeError):
    pass


#: The debug interfaces a probe can be asked for. SWD is the default because
#: every probe that does JTAG also does SWD, and the on-board STLINK-V3E of a
#: NUCLEO-H743ZI does only SWD (measured 2026-10-01).
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


def explain_connect_failure(info: TargetInfo, interface: str = SWD) -> str:
    """Turn an unhelpful CLI error into the two things it usually means.

    This exists because 'Unable to get core ID' is emitted for both a wrong
    access port and a boot configuration that closes the debug port, and those
    have completely different fixes.
    """
    if info.connected:
        return ""
    if any("Serial number not found" in e for e in info.errors):
        return (
            "No probe with that serial is attached; the programmer said so. "
            "list_probes shows the attached probes and their serials."
        )
    hints = []
    if interface == JTAG:
        hints.append(
            "The probe may not do JTAG, or not be wired for it. A probe that "
            "cannot answers exactly as if no target were attached: the on-board "
            "STLINK-V3E of a NUCLEO-H743ZI reports 'Unable to get core ID' over "
            "JTAG while SWD works. Try interface 'swd', or a probe on the "
            "target's JTAG connector."
        )
    hints += [
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
        interface: str = SWD,
        try_first: int | None = None,
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
        # inspected. HWRSTPULSE second: it pulses the reset line, which RECOVERS
        # an access port that running firmware has closed, at the cost of
        # restarting the target.
        modes = ["HOTPLUG", "HWRSTPULSE"] if allow_reset else ["HOTPLUG"]

        for mode in modes:
            for port in ports:
                if mode != "HOTPLUG":
                    # Recorded before the connect, not after it succeeds: the
                    # pulse restarts the target whether or not the port answers.
                    self.target_was_reset = True
                result = self._run(self._connect_args(port, mode), timeout=90)
                info = parse_target_info(result.stdout)
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
        if allow_reset and "HWRSTPULSE" not in attempts:
            attempts.append("HWRSTPULSE")

        for attempt in attempts:
            if attempt != "HOTPLUG":
                # As in identify(): a resetting connect that fails still reset
                # the target. On an STM32N6 whose image has closed AP1 this
                # branch pulses the board and then fails, and it used to report
                # target_was_reset False for a board it had just restarted.
                self.target_was_reset = True
            result = self._run(self._connect_args(wanted, attempt), timeout=90)
            if parse_target_info(result.stdout).connected:
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
        ]
        result = self._run(argv, timeout=90)
        words = parse_memory(result.stdout)
        if not words:
            raise ProbeError(
                f"No data read at 0x{address:08X}. Output was:\n{result.stdout.strip()}"
            )
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
        argv = self._connect_args(port, "HOTPLUG") + ["-r32", f"0x{address:08X}", str(length_bytes)]
        result = self._run(argv, timeout=90)
        info = parse_target_info(result.stdout)
        if not info.connected:
            return None
        quirks = info.quirks
        if quirks.access_port != port or not quirks.is_measured("access_port"):
            return None
        self._access_port = port
        self.connect_mode = "HOTPLUG"
        self.access_port_provenance = quirks.provenance_of("access_port")
        words = parse_memory(result.stdout)
        if not words:
            raise ProbeError(
                f"No data read at 0x{address:08X}. Output was:\n{result.stdout.strip()}"
            )
        return info, words

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
        Measured on a NUCLEO-H743ZI 2026-08-16, whose reset handler is at
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

        "Core run" is what the CLI prints on success, observed on a NUCLEO-H743ZI
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
            raise ProbeError(f"Programming failed:\n{result.stdout.strip()}")
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
        mapping[match.group(1)] = match.group(2)
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


def list_probes(toolchain: Toolchain) -> list[ProbeInfo]:
    result = run_command([str(toolchain.programmer), "-l"], timeout=90)
    return parse_probe_list(result.stdout)


def list_probes_with_unreadable(
    toolchain: Toolchain,
) -> tuple[list[ProbeInfo], int, list[str]]:
    """Readable probes, how many entries could not be read at all, and the error
    lines the programmer printed in its ST-LINK section."""
    result = run_command([str(toolchain.programmer), "-l"], timeout=90)
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
