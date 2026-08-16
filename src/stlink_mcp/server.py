"""The MCP tool surface.

Design rule for everything in this file: a tool reports success only after
checking that the thing it was asked to do actually happened. The underlying
vendor tools do not do this. STM32_Programmer_CLI will print "Start operation
achieved successfully" for a start that left the program counter untouched, and
a GDB load against an already-running target completes cleanly while changing
nothing. An agent driving those CLIs directly inherits both lies.

So: "ran" means the target produced output, not that a command exited zero.
Where a claim cannot be verified, the result says so rather than implying it
was checked.

That rule is enforced by construction rather than by care: no tool in this file
sets `ok`. Every return goes through `result.verified`, `result.unverified` or
`result.failed`, and the first of those will not build without the evidence that
justifies it. See `result.py` for why that became necessary.

With more than one board attached, every tool takes a `probe_serial`. Leaving it
out is only safe with a single probe, and the tools say so rather than quietly
picking one, because "the first probe" is not stable across replugs and loading
firmware onto the wrong board is a failure that looks like success.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp.server import MCPServer

from . import console as console_mod
from . import quirks as quirks_mod
from .result import failed, unverified, verified
from .probe import (
    Probe,
    ProbeInfo,
    TargetInfo,
    explain_connect_failure,
    list_probes as _list_probes,
    list_probes_with_unreadable,
    reset_landed_at_vector,
    reset_vector_address,
)
from .runner import (
    RunError,
    port_is_taken,
    acquire_server,
    held_serials,
    load_and_run,
    release_server,
    server_is_held,
)
from .toolchain import Toolchain, ToolchainError, find_toolchain

mcp = MCPServer("stlink-mcp")

_GDB_PORT_BASE = int(os.environ.get("STLINK_MCP_GDB_PORT", "61234"))

# One GDB server instance opens TWO ports: the one given with -p, and -p + 1 for
# SWO. Measured, not assumed: `ss -ltnp` against a running server shows both.
# With a stride of 1, the second board's GDB port lands exactly on the first
# board's SWO port, which is how a four-board rig fails on its second load.
_GDB_PORT_STRIDE = 4
_CONSOLE_BAUD = int(os.environ.get("STLINK_MCP_CONSOLE_BAUD", "115200"))

# Cached per probe while its GDB server is held. Re-identifying would mean
# running STM32_Programmer_CLI, which cannot reach a probe the server has.
_held: dict[str | None, tuple[Probe, TargetInfo, ProbeInfo | None]] = {}


def _cached_for(serial: str | None):
    """The cached identity, but only while a live server actually holds it.

    The cache exists because a held server blocks the programmer CLI. Once that
    server dies the reason is gone, so the entry must go too: otherwise the
    identity of a board nobody is holding keeps being served from memory long
    after it stopped being true.
    """
    if serial in _held and server_is_held(serial):
        return _held[serial]
    _held.pop(serial, None)
    return None


_HELD_MESSAGE = (
    "A GDB server is holding THIS probe, so the programmer cannot connect to "
    "it. Enumeration and other probes are unaffected. The server is kept alive "
    "because on some parts a typical image leaves the debug access port "
    "unreachable once it has run. Call release_probe() to free it. If your "
    "image is one of those, the board then needs a power cycle before firmware "
    "can run again, and board_state reports the family note describing what in "
    "the image causes it, which is usually one line and usually removable."
)


def _toolchain() -> Toolchain:
    toolchain = find_toolchain()
    toolchain.check()
    return toolchain


def _resolve_probe(
    toolchain: Toolchain, probe_serial: str | None
) -> tuple[str | None, ProbeInfo | None, str | None]:
    """Pick which probe to talk to. Returns (serial, info, error).

    Enumeration is run even when a server is held. This used to be skipped in
    favour of a cache, on the belief that a held server blocks the programmer
    CLI outright. Measured 2026-08-15 with five probes attached and a server
    held: `STM32_Programmer_CLI -l` listed all five in 1.5 seconds, the same as
    with nothing held. What a held server actually blocks is a CONNECT to the
    probe it holds, which is a much narrower thing and is checked per probe by
    the tools that need it.
    """
    attached = _list_probes(toolchain)
    if not attached:
        return None, None, "No ST-LINK probes found."

    if probe_serial is None:
        if len(attached) > 1:
            listing = ", ".join(p.serial for p in attached)
            return None, None, (
                f"{len(attached)} probes are attached and no probe_serial was given. "
                f"Pass one of: {listing}. Not choosing for you, because probe order "
                "is not stable across replugs and using the wrong board is a "
                "failure that looks like success."
            )
        return attached[0].serial, attached[0], None

    for info in attached:
        if info.serial == probe_serial:
            return info.serial, info, None

    listing = ", ".join(p.serial for p in attached)
    return None, None, f"No probe with serial {probe_serial}. Attached: {listing}"


# Assigned on first use and never reassigned, so a probe keeps its port for the
# life of the process.
_gdb_ports: dict[str | None, int] = {}


def _gdb_port_for(serial: str | None) -> int:
    """A distinct GDB port per probe.

    Deliberately a registry rather than a function of the serial. An earlier
    version derived the port from hash(serial) % 100, which is wrong twice over:
    Python randomises string hashing per process, so ports moved between runs,
    and nothing prevented two serials colliding on one port. With four boards
    attached that collision showed up on the fourth, as a refusal to start a
    second server on a port already in use.

    Ports already in use are stepped over rather than collided with. Counting
    from the base was enough while this was the only thing on the machine, and
    stopped being enough the moment a second instance existed: another copy of
    this server, an IDE, or the project's own dev harness starts numbering from
    the same base, so the newcomer's first probe lands on the incumbent's first
    probe and the run fails with "port already in use" for a reason that has
    nothing to do with the board. Both ports of the pair are checked, since a
    server opens -p and -p+1.
    """
    if serial in _gdb_ports:
        return _gdb_ports[serial]

    taken = set(_gdb_ports.values())
    port = _GDB_PORT_BASE
    ceiling = _GDB_PORT_BASE + _GDB_PORT_STRIDE * 64
    while port < ceiling:
        if (
            port not in taken
            and not port_is_taken(port)
            and not port_is_taken(port + 1)
        ):
            _gdb_ports[serial] = port
            return port
        port += _GDB_PORT_STRIDE

    raise RunError(
        f"No free GDB port pair between {_GDB_PORT_BASE} and {ceiling}. Something "
        "is holding a great many of them: `ss -ltnp` will say what. "
        "STLINK_MCP_GDB_PORT moves the base."
    )


@mcp.tool()
def list_probes() -> dict[str, Any]:
    """List every attached ST-LINK, with its firmware and its serial console.

    The serial-to-console mapping is the useful part with more than one board:
    it is not positional, so pairing probes with /dev/ttyACM* by order gets it
    wrong.

    Works while other probes are held. This used to refuse outright whenever any
    server was held, which was based on an untested assumption; enumeration is
    not in fact blocked, so the held probes are simply marked.
    """
    try:
        toolchain = _toolchain()
    except ToolchainError as exc:
        return failed(str(exc))

    held = set(held_serials())
    probes, unreadable = list_probes_with_unreadable(toolchain)
    detail: dict[str, Any] = {}
    if unreadable:
        detail["unreadable_entries"] = unreadable
        detail["unreadable_note"] = (
            f"{unreadable} probe entr{'y was' if unreadable == 1 else 'ies were'} "
            "listed by the programmer but could not be read, which usually means a "
            "USB problem rather than anything about the target. They are excluded "
            "above rather than passed on, because the placeholder the tool prints "
            "in place of a serial is not one."
        )
    return verified(
        "programmer enumeration",
        **detail,
        count=len(probes),
        probes=[
            {
                "serial": p.serial,
                "firmware": p.firmware,
                "board": p.board,
                "console_port": p.console_port,
                # 0 means the probe answered but could not reach a target, which
                # is what a closed debug port looks like from out here.
                "access_ports": p.access_ports,
                # A held probe still enumerates. What it will not do is accept a
                # programmer connect until release_probe() frees it.
                "gdb_server_held": p.serial in held,
            }
            for p in probes
        ],
    )


@mcp.tool()
def board_state(probe_serial: str | None = None) -> dict[str, Any]:
    """Identify a target and report what is known about it.

    When the probe is free, this tries each debug access port, because a wrong
    access port and an absent target produce the same error message from the
    vendor tools.

    When a GDB server is holding the probe it cannot do that, because the
    programmer CLI cannot reach a held probe. It then answers from what was
    captured when the server was acquired and says so, rather than presenting a
    remembered identity as a fresh reading of the hardware.
    """
    try:
        toolchain = _toolchain()
    except ToolchainError as exc:
        return failed(str(exc))

    serial, probe_info, error = _resolve_probe(toolchain, probe_serial)
    if error:
        return failed(error)

    entry = _cached_for(serial)
    cached = entry is not None
    if entry is not None:
        probe, info, probe_info = entry
    else:
        probe = Probe(toolchain, serial=serial)
        info = probe.identify()

    quirks = info.quirks
    state: dict[str, Any] = {
        "probe_serial": serial,
        "stlink": {"serial": info.stlink_serial, "firmware": info.stlink_firmware},
        "board": info.board,
        "device": {
            "id": f"0x{info.device_id:X}" if info.device_id is not None else None,
            "name": info.device_name,
            "cpu": info.cpu,
            "revision": info.revision,
        },
        "access_port": probe.access_port,
        "connect_mode": probe.connect_mode,
        "target_was_reset": probe.target_was_reset,
        "family": {
            "name": quirks.name,
            "requires_signing_for_flash": quirks.requires_signing,
            "ram_load_base": _hex(quirks.ram_load_base),
            "vector_table": _hex(quirks.vector_table_addr),
            "flash_base": _hex(quirks.flash_base),
            "external_loader": quirks.external_loader,
            "notes": quirks.notes,
            # Which of these numbers somebody has actually seen work on a board.
            # Reported beside the values rather than folded into them, because
            # an agent deciding whether to trust an address needs both, and a
            # value with no provenance reads as a measured one.
            "provenance": quirks.provenance_summary(),
        },
        "console_port": probe_info.console_port if probe_info else None,
        "gdb_server_held": server_is_held(serial),
    }

    if not info.connected:
        return failed(
            "Could not reach the core.",
            errors=info.errors,
            hint=explain_connect_failure(info),
            **state,
        )

    if cached:
        return unverified(
            "A GDB server is holding this probe, so nothing was run against the "
            "hardware. This is the identity captured when that server was "
            "acquired, which may be some time ago: a board that has since "
            "crashed, browned out or been unplugged looks identical here.",
            **state,
        )

    if probe.degraded_reason:
        return unverified(probe.degraded_reason, **state)

    return verified("programmer connect", **state)


@mcp.tool()
def run_firmware(
    elf_path: str,
    probe_serial: str | None = None,
    console_seconds: float = 3.0,
) -> dict[str, Any]:
    """Load an ELF into target RAM, start it, and verify that it started.

    Verification is console output from THAT board's own serial port, captured
    from before the target is resumed so start-up banners are not missed.

    Two weaker outcomes are reported honestly rather than as success. If the GDB
    batch does not reach its end markers the load did not complete, and any
    console output is the previous image still running. If the load completes but
    nothing arrives on the console, that is reported as unverified rather than as
    a run, because a target that is silent and a target that does not print look
    identical from here.

    Does not sign anything: an image placed directly in RAM by a debugger
    bypasses the boot ROM.
    """
    try:
        toolchain = _toolchain()
    except ToolchainError as exc:
        return failed(str(exc))

    serial, probe_info, error = _resolve_probe(toolchain, probe_serial)
    if error:
        return failed(error)

    entry = _cached_for(serial)
    if entry is not None:
        probe, info, probe_info = entry
    else:
        probe = Probe(toolchain, serial=serial)
        info = probe.identify()
        if not info.connected:
            return failed(
                "No target",
                probe_serial=serial,
                errors=info.errors,
                hint=explain_connect_failure(info),
            )
        # A degraded programmer connect is NOT a reason to refuse a run. The GDB
        # server uses its own path and reaches AP1 when the programmer cannot.

    quirks = info.quirks
    if quirks.vector_table_addr is None:
        return failed(
            f"No RAM-execution layout is recorded for {quirks.name}. "
            "Add it to quirks.py, or supply it for this device, and say where "
            "the value came from rather than guessing."
        )

    elf = Path(elf_path).expanduser()
    # This board's own console, not whichever ttyACM* happens to be first.
    port = probe_info.console_port if probe_info else console_mod.find_console_port()
    gdb_port = _gdb_port_for(serial)
    console_error: str | None = None

    try:
        # Family's port, not the one the programmer settled on: see
        # Probe.gdb_access_port for why those differ.
        acquire_server(toolchain, probe.gdb_access_port(info), gdb_port, serial=serial)
        _held[serial] = (probe, info, probe_info)

        if port:
            with console_mod.ConsoleCapture(port, _CONSOLE_BAUD) as cap:
                sp, pc, size, _ = load_and_run(
                    toolchain, elf, quirks.vector_table_addr,
                    probe.gdb_access_port(info), gdb_port
                )
                try:
                    text = cap.collect(console_seconds)
                except Exception as exc:  # noqa: BLE001
                    # A USB replug mid-capture raises SerialException, which is
                    # an OSError and so is not the ConsoleError the outer clause
                    # catches. Letting it out would discard work already done and
                    # already known good: the load completed, the markers are in
                    # hand, and the server is now HELD. Keep the partial buffer
                    # and report the interruption instead of losing all of it.
                    text = cap.text
                    console_error = str(exc)
        else:
            sp, pc, size, _ = load_and_run(
                toolchain, elf, quirks.vector_table_addr,
                probe.gdb_access_port(info), gdb_port
            )
            text = ""
    except (RunError, ToolchainError, console_mod.ConsoleError) as exc:
        return failed(str(exc), probe_serial=serial)

    detail: dict[str, Any] = {
        "probe_serial": serial,
        "console_port": port,
        "image_initial_sp": _hex(sp),
        "image_entry_point": _hex(pc),
        "load_size_bytes": size,
        "console": text,
        "image_note": (
            "image_initial_sp and image_entry_point are read out of the ELF's own "
            "vector table and written into the core. They are what the image asked "
            "for, not a reading of where the core is. core_registers() is the "
            "observation."
        ),
    }

    if quirks.debug_closes_on_run:
        detail["next_run_note"] = (
            f"On {quirks.name}, a typical image leaves the debug access port "
            "unreachable once it has run, and no reset reopens it. The GDB server "
            "is therefore being HELD so further run_firmware calls on this board "
            "keep working without a power cycle. read_memory, core_registers and "
            "flash_firmware cannot reach the probe until release_probe() is called. "
            "This is a property of the image, not of the silicon: see the family "
            "notes in board_state for what causes it and how to remove it."
        )

    # The markers are the only evidence that the GDB batch reached its end. GDB
    # in -batch mode aborts the whole script on the first failed command, so a
    # missing marker means the load did not happen, whatever the console says.
    # This matters most when it looks best: the previous image is still resident
    # and still printing, so the console is busy while nothing was loaded.
    if sp is None or pc is None:
        return failed(
            "The GDB batch did not reach its end markers, so the load did not "
            "complete. Any console output here is the PREVIOUS image still "
            "running, not the one just requested.",
            **detail,
        )

    if console_error is not None:
        detail["console_error"] = console_error
        if text.strip():
            return verified(
                "partial console output from this board's own port, before the "
                "capture was interrupted",
                **detail,
            )
        return unverified(
            f"The load completed, but the console could not be read: "
            f"{console_error}. Nothing observed whether the image started.",
            **detail,
        )

    if not port:
        return unverified(
            "No serial console is mapped to this probe, so nothing observed "
            "whether the image started. The load itself completed.",
            **detail,
        )

    if not text.strip():
        return unverified(
            "The image loaded and was started, but the target printed nothing in "
            "the capture window. It may not be running, or it may not print. "
            "core_registers() shows where the program counter actually is, after "
            "release_probe().",
            **detail,
        )

    return verified("console output from this board's own port", **detail)


@mcp.tool()
def release_probe(probe_serial: str | None = None) -> dict[str, Any]:
    """Shut down held GDB servers and free the probes.

    With no serial, releases all of them. Needed before read_memory,
    core_registers or flash_firmware can be used after a run. On parts where the
    access port is left unreachable by a typical image, this is a one-way door
    until the board is power cycled, so it is never done implicitly. That is a
    property of the image and removable in it; see the family notes.
    """
    affected = [probe_serial] if probe_serial is not None else list(_held)
    quirks = [_held[s][1].quirks for s in affected if s in _held]

    released = release_server(probe_serial)
    for s in affected:
        _held.pop(s, None)

    detail: dict[str, Any] = {"released": released, "probes": affected}
    if released and any(q.debug_closes_on_run for q in quirks):
        detail["warning"] = (
            "The debug access port cannot be re-acquired now that firmware has "
            "run. Power cycle the affected board(s) before the next run_firmware. "
            "This is caused by the image, not the silicon: board_state carries the "
            "family note saying what to change so it stops happening."
        )

    if not released:
        return unverified(
            "No GDB server was held for that probe, so nothing was released. "
            "Either it was already free, or a different serial is holding it.",
            **detail,
        )

    return verified("gdb server shut down and the probe freed", **detail)


@mcp.tool()
def flash_firmware(
    image_path: str, probe_serial: str | None = None, address: int | None = None
) -> dict[str, Any]:
    """Program a signed image into a target's boot memory for standalone boot.

    Prefer run_firmware for edit-build-run: it is faster and needs no signing.
    This tool does not sign, because signing needs keys and options that belong
    to the project rather than to a programmer.
    """
    try:
        toolchain = _toolchain()
    except ToolchainError as exc:
        return failed(str(exc))

    serial, _, error = _resolve_probe(toolchain, probe_serial)
    if error:
        return failed(error)
    # Only this probe matters. Another board's held server does not stop us.
    if server_is_held(serial):
        return failed(_HELD_MESSAGE, probe_serial=serial, held_serials=held_serials())

    probe = Probe(toolchain, serial=serial)
    info = probe.identify()
    if not info.connected:
        return failed("No target", hint=explain_connect_failure(info))

    quirks = info.quirks
    target_address = address if address is not None else quirks.flash_base
    if target_address is None:
        return failed(
            f"No flash base recorded for {quirks.name}; pass address explicitly."
        )

    # The write path does not act on a value nobody has measured.
    #
    # Elsewhere this server will act on an unmeasured address and let the
    # observation decide, because a RAM load that used the wrong base is undone
    # by a reset and there IS an observation: the target either produces console
    # output or it does not. Neither is true here. This tool writes persistently
    # and has no readback, so a wrong base would be reported by the only check
    # available, the programmer's own banner, which is the specific thing this
    # project exists to distrust.
    #
    # An explicit `address` is a different matter and is allowed: the caller has
    # said where to write, so it is their claim rather than the table's guess.
    if address is None and not quirks.is_measured("flash_base"):
        return failed(
            f"The flash base for {quirks.name} is "
            f"{quirks.provenance_of('flash_base')}, not measured on hardware, and "
            "this tool writes permanently with no readback to catch a wrong "
            "address.",
            hint=(
                "Either pass `address` explicitly, which records the choice as "
                "yours, or confirm the base by running an image from RAM first, "
                "which is reversible and self-verifying."
            ),
        )

    try:
        output = probe.program_flash(
            str(Path(image_path).expanduser()), target_address, quirks.external_loader
        )
    except Exception as exc:  # noqa: BLE001
        return failed(str(exc))

    # The evidence names its own weakness deliberately. This is the one tool
    # whose only check is the vendor's own banner, which is exactly the thing
    # the README's opening example shows cannot be trusted on its own.
    return verified(
        "the programmer reported the download completed. The image was NOT read "
        "back and compared, so this confirms the write was accepted, not that "
        "flash holds what you sent",
        probe_serial=serial,
        address=_hex(target_address),
        signing_required=quirks.requires_signing,
        detail=output.strip().splitlines()[-3:],
        next_step=(
            "This does not start the image. Set the board's boot configuration to "
            "boot from flash and reset it."
        ),
    )


@mcp.tool()
def read_memory(
    address: int, length_bytes: int = 32, probe_serial: str | None = None
) -> dict[str, Any]:
    """Read target memory as 32-bit words, without disturbing the target.

    Connects hotplug-only, so it neither halts nor resets the core. That is a
    real constraint rather than a preference: on a part where running firmware
    has closed the family's debug access port, reopening it needs a reset, and a
    reset destroys the very state you are reading memory to inspect. This
    refuses instead, and says why.
    """
    try:
        toolchain = _toolchain()
        serial, _, error = _resolve_probe(toolchain, probe_serial)
        if error:
            return failed(error)
        if server_is_held(serial):
            return failed(
                _HELD_MESSAGE, probe_serial=serial, held_serials=held_serials()
            )
        probe = Probe(toolchain, serial=serial)
        info = probe.identify(allow_reset=False)
        if not info.connected:
            return failed(
                "Could not reach the core without resetting it, and this tool "
                "does not reset. core_registers() will reset if it must, at the "
                "cost of the state you are inspecting.",
                probe_serial=serial,
                hint=explain_connect_failure(info),
            )
        words = probe.read_memory(address, length_bytes)
    except Exception as exc:  # noqa: BLE001
        return failed(str(exc))

    detail: dict[str, Any] = {
        "probe_serial": serial,
        "connect_mode": probe.connect_mode,
        "target_was_reset": probe.target_was_reset,
        "words": {f"0x{a:08X}": f"0x{v:08X}" for a, v in sorted(words.items())},
    }
    if probe.degraded_reason:
        return unverified(probe.degraded_reason, **detail)

    return verified("words read back from the target", **detail)


#: Where a user describes boards this project has not characterised.
_BOARDS_ENV = "STLINK_MCP_BOARDS"
_BOARDS_DEFAULT = Path.home() / ".config" / "stlink-mcp" / "boards.toml"


def board_config_path() -> Path | None:
    """The board file to load, or None if the user has not written one.

    An explicit `STLINK_MCP_BOARDS` is honoured even when it does not exist, so
    that a typo in the variable is an error rather than a silent fallback to the
    default. Asking for a specific file and quietly getting a different set of
    boards is the shape of failure this project exists to remove.
    """
    env = os.environ.get(_BOARDS_ENV)
    if env:
        return Path(env).expanduser()
    return _BOARDS_DEFAULT if _BOARDS_DEFAULT.exists() else None


def load_board_config() -> tuple[int, Path | None]:
    """Install user-described boards. Raises BoardConfigError if the file is bad.

    Called at start-up rather than lazily, and it is allowed to stop the server.
    A user who wrote a board file is telling this server what the hardware on
    their desk is; running anyway, with those boards absent, would answer every
    later question about a different machine than the one they described.
    """
    path = board_config_path()
    if path is None:
        return 0, None
    boards = quirks_mod.load_user_boards(path)
    quirks_mod.set_user_boards(boards)
    return len(boards), path


@mcp.tool()
def reset_target(
    probe_serial: str | None = None, leave_halted: bool = False
) -> dict[str, Any]:
    """Reset the target, and check that it actually restarted.

    Reset is one of the three things this server promises and it used to happen
    only as a side effect of something else, which meant nobody could ask for it
    and nobody was told whether it worked.

    How it verifies, and why it is done this way. The reset is performed by the
    CONNECT, using HWRSTPULSE, rather than by issuing `-rst` afterwards.
    Measured on a NUCLEO-H743ZI whose reset handler is at 0x0807FA98: a hotplug
    connect followed by `-rst -halt` leaves the PC at 0x0807FC2C, and the
    reverse order leaves it at 0x0807FC3A. Both reset the part correctly and
    then lose the race to halt it, so the core is already hundreds of bytes into
    start-up and its PC cannot be compared against anything. A resetting connect
    lands on the vector exactly, every time, which is what makes the check
    possible at all.

    The core is halted to read the PC and then released, unless you ask for it
    to stay halted. That brief halt is reported rather than hidden, because it
    is a state change the caller did not ask for.
    """
    try:
        toolchain = _toolchain()
        serial, _, error = _resolve_probe(toolchain, probe_serial)
        if error:
            return failed(error)
        if server_is_held(serial):
            return failed(
                _HELD_MESSAGE, probe_serial=serial, held_serials=held_serials()
            )

        probe = Probe(toolchain, serial=serial)
        # allow_reset=False deliberately: identification must not reset the
        # target before this tool has decided to, or the thing being verified
        # has already happened once, unobserved.
        info = probe.identify(allow_reset=False)
        if not info.connected:
            return failed(
                "Cannot reach the target to reset it.",
                probe_serial=serial,
                hint=explain_connect_failure(info),
            )

        quirks = info.quirks
        access_port = quirks.access_port

        # Read the expected reset handler BEFORE resetting. Afterwards is too
        # late on any part where the reset closes the debug port.
        expected: int | None = None
        vector_addr = reset_vector_address(quirks)
        if vector_addr is not None:
            try:
                words = probe.read_memory(vector_addr, 8)
                expected = words.get(vector_addr + 4)
            except Exception:  # noqa: BLE001
                expected = None

        pc, output = probe.reset_and_halt(access_port)
    except Exception as exc:  # noqa: BLE001
        return failed(str(exc))

    detail: dict[str, Any] = {
        "probe_serial": serial,
        "family": quirks.name,
        "reset_kind": "hardware pulse on nRST (HWRSTPULSE connect)",
        "program_counter": _hex(pc),
        "expected_reset_handler": _hex(None if expected is None else expected & ~1),
        "core_halted": pc is not None and leave_halted,
    }

    if pc is None:
        # The pulse went out and the debug port did not answer afterwards.
        #
        # DO NOT read this as "the reset closed the port". That was the first
        # conclusion drawn here on 2026-08-16 and it was wrong, from exactly the
        # mistake this project keeps writing about: one board was pulsed, a
        # different board answered, and the difference was attributed to the
        # pulse. The proper test is a board confirmed open immediately before.
        # Run that way, a NUCLEO-N657X0-Q pulsed, halted in its boot ROM at
        # 0x18003A1A, and its port was STILL OPEN afterwards, because a
        # reset-and-halt catches the core before any image has run and so before
        # anything gates the clock that keeps the port alive. The board that
        # failed had been closed before it was touched.
        #
        # So the honest reading is the narrow one: nothing was observed. The
        # likeliest cause is a port that was already closed, which a reset does
        # not reopen on this family.
        reason = (
            "the reset pulse was issued, but the debug port did not answer "
            "afterwards, so nothing observed the target restart"
        )
        if quirks.debug_closes_on_run:
            detail["note"] = (
                f"On {quirks.name} the debug access port is most likely to have "
                "been closed ALREADY, by an image that ran before this call, and "
                "a reset does not reopen it on this family. Only removing power "
                "does. This is not evidence that the reset failed, and it is not "
                "evidence that it worked."
            )
        return unverified(reason, detail_output=output.strip().splitlines()[-3:],
                          **detail)

    if expected is None:
        return unverified(
            "the core halted after a reset pulse, but this part's post-reset PC "
            "is not predictable from the quirks table, so nothing confirms it "
            "restarted at its reset vector",
            hint=(
                "This is normal for a part that boots a signed image out of "
                "external memory: the core comes up inside the boot ROM, at an "
                "address this table does not record."
            ),
            **detail,
        )

    if not reset_landed_at_vector(pc, expected):
        return unverified(
            f"the core halted at {_hex(pc)}, which is not this part's reset "
            f"handler {_hex(expected & ~1)}, so the reset did not leave the "
            "target where a reset should",
            **detail,
        )

    if not leave_halted:
        try:
            probe.release_core(access_port)
            detail["core_halted"] = False
            detail["note"] = (
                "The core was halted only long enough to read the program "
                "counter, and is running again."
            )
        except Exception as exc:  # noqa: BLE001
            detail["core_halted"] = True
            detail["note"] = f"The core is still HALTED: releasing it failed ({exc})."

    return verified(
        f"the core halted at {_hex(pc)}, which is the reset handler named by "
        f"word 1 of the vector table at {_hex(vector_addr)}",
        **detail,
    )


@mcp.tool()
def core_registers(probe_serial: str | None = None) -> dict[str, Any]:
    """Read the core registers. This HALTS the core.

    The most direct way to find out what a program is really doing, and what
    settles "did it actually start" when there is no console. Perturbing, so it
    is never used inside the routine checks.
    """
    try:
        toolchain = _toolchain()
        serial, _, error = _resolve_probe(toolchain, probe_serial)
        if error:
            return failed(error)
        if server_is_held(serial):
            return failed(
                _HELD_MESSAGE, probe_serial=serial, held_serials=held_serials()
            )
        probe = Probe(toolchain, serial=serial)
        probe.identify()
        regs = probe.core_registers()
    except Exception as exc:  # noqa: BLE001
        return failed(str(exc))

    detail: dict[str, Any] = {
        "probe_serial": serial,
        "halted": True,
        "connect_mode": probe.connect_mode,
        # Reported, not hidden. Reaching the family's access port on a part that
        # closes it can need a resetting connect, and a reset restarts the very
        # program being inspected. A caller cannot allow for that if it is not told.
        "target_was_reset": probe.target_was_reset,
        "registers": {name: f"0x{value:08X}" for name, value in regs.items()},
        "note": "The core is now halted. Resume it by running firmware again.",
    }
    if probe.target_was_reset:
        detail["reset_note"] = (
            "The target was RESET to reach its debug access port, so these "
            "registers describe a freshly restarted program, not the one that "
            "was running when you asked."
        )
    if probe.degraded_reason:
        return unverified(probe.degraded_reason, **detail)

    return verified("registers read back from the halted core", **detail)


@mcp.tool()
def capture_console(
    seconds: float = 5.0, probe_serial: str | None = None
) -> dict[str, Any]:
    """Capture what a target prints on its serial console.

    Resolves the port from the probe's serial rather than taking the first
    /dev/ttyACM*, which with two boards attached is the wrong one about half the
    time, and with the five attached while this was written is a one-in-five pick.

    Refuses rather than guessing, like every other tool here. It used to compute
    the ambiguity error and then discard it, falling through to the first
    matching device path: the one behaviour its own docstring said it avoided.
    Reading the wrong board is not harmless for a verification tool, because
    silence from the wrong board reads as "firmware not running" and output from
    the wrong board reads as "it works".
    """
    try:
        toolchain = _toolchain()
    except ToolchainError as exc:
        return failed(str(exc))

    serial, probe_info, error = _resolve_probe(toolchain, probe_serial)
    if error:
        return failed(error)

    port = probe_info.console_port if probe_info else None
    if port is None:
        port = os.environ.get("STLINK_MCP_CONSOLE")
    if port is None:
        return failed(
            f"No serial console is mapped to probe {serial}: the probe list's "
            "UART section did not name a port for it. Set STLINK_MCP_CONSOLE to "
            "the device path if you know it. This does not fall back to the "
            "first /dev/ttyACM*, because that path belongs to whichever board "
            "enumerated first, not to this one.",
            probe_serial=serial,
        )

    try:
        text = console_mod.capture(port, seconds, _CONSOLE_BAUD)
    except console_mod.ConsoleError as exc:
        return failed(str(exc), probe_serial=serial, port=port)

    return verified(
        "serial console read from the port mapped to this probe's serial",
        probe_serial=serial,
        port=port,
        seconds=seconds,
        output=text,
    )


def _hex(value: int | None) -> str | None:
    return None if value is None else f"0x{value:08X}"
