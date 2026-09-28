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
from . import faults as faults_mod
from . import image as image_mod
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
from .vendordb import VendorDbError, seed_from_directory

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
    """List every attached ST-LINK with its firmware, board name and serial console.

The console mapping is the useful part with more than one board: it is not
positional, so pairing probes with /dev/ttyACM* by order gets it wrong. Works
while other probes are held by a GDB server; those are marked as held.
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
    """Identify the target, the probe and the target's family.

Tries access ports 0 and 1. If a hotplug connect cannot reach the core, or the
family's recorded port, it retries with a resetting connect and sets
`target_was_reset`. While a GDB server holds the probe it answers from cache
and reports `unverified`. Check `family.provenance`: only `measured` values
have been seen to work. `access_port_warning` means the port is a guess;
identity reads fine on a port that cannot halt the core.
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
        # The mode that CONNECTED. When nothing did, naming the default would
        # read as a hotplug connect that worked.
        "connect_mode": probe.connect_mode if info.connected else None,
        "target_was_reset": probe.target_was_reset,
        "family": {
            "name": quirks.name,
            # These keys are the FamilyQuirks field names on purpose. They used
            # to be prettier - "vector_table", "requires_signing_for_flash" -
            # and provenance_summary() keys by field name, so the two halves of
            # the same fact could not be joined by anything reading the payload.
            # A provenance map whose keys do not match the values it describes is
            # worse than no provenance map.
            "requires_signing": quirks.requires_signing,
            "ram_load_base": _hex(quirks.ram_load_base),
            "vector_table_addr": _hex(quirks.vector_table_addr),
            "flash_base": _hex(quirks.flash_base),
            "external_loader": quirks.external_loader,
            # Reported because provenance_summary() reports where this value came
            # from, and a provenance with no value beside it tells an agent
            # nothing. It also decides whether a later run_firmware will cost a
            # power cycle, which is worth knowing beforehand rather than after.
            "debug_closes_on_run": quirks.debug_closes_on_run,
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

    # A part the databases describe and this refused to seed is NOT the same as a
    # part nothing knows about, and until now both produced the generic entry with
    # identical notes. The reason names the candidate addresses, which is exactly
    # what someone writing a boards.toml entry for that part needs.
    refusal = quirks_mod.vendor_refusal(info.device_id)
    if refusal:
        state["family"]["vendor_database_refused"] = refusal
        state["family"]["how_to_proceed"] = (
            "STMicroelectronics' database does describe this part; this server "
            "refused to seed it rather than choose for you. Put the values you "
            "want in a board file (see STLINK_MCP_BOARDS) and they will be used, "
            "labelled user-stated."
        )

    if not cached and probe.access_port_provenance is not None:
        state["access_port_provenance"] = probe.access_port_provenance
    if not cached and probe.unmeasured_port_warning:
        state["access_port_warning"] = probe.unmeasured_port_warning
        if probe.other_ports_that_answered:
            state["other_access_ports_that_answered"] = probe.other_ports_that_answered

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

Verified only by console output from that board's serial console: `unverified`
if it stays silent, `failed` if the load did not complete. Refuses, before any
load, an image whose vector table is not at the family's RAM address or with a
segment outside SRAM (0x20000000-0x3FFFFFFF), since a GDB load writes wherever
the image says. Resets the target first and keeps the GDB server; see
`release_probe`. No signing.
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
                target_was_reset=probe.target_was_reset,
                errors=info.errors,
                hint=explain_connect_failure(info),
            )
        # A degraded programmer connect is NOT a reason to refuse a run. The GDB
        # server uses its own path and reaches AP1 when the programmer cannot.

    quirks = info.quirks
    # Disclosed on the SUCCESS path, not only on a refusal.
    #
    # flash_firmware refuses an unmeasured base and names the provenance in the
    # refusal, so a caller there cannot miss it. This tool ACTS on an unmeasured
    # load address by design - a wrong one is undone by a reset and the console is
    # a real observation - and it used to return `verified` with no hint that the
    # address came from ST's database rather than a board. An agent could then
    # honestly write "confirmed this part's RAM layout" from a number nobody has
    # measured, which is the precise harm this project exists to prevent.
    layout_provenance = quirks.provenance_of("vector_table_addr")
    if quirks.vector_table_addr is None:
        return failed(
            f"No RAM-execution layout is recorded for {quirks.name}. "
            "Add it to quirks.py, or supply it for this device, and say where "
            "the value came from rather than guessing."
        )

    elf = Path(elf_path).expanduser()
    if not elf.exists():
        return failed(f"No such ELF: {elf}")
    # A GDB load writes wherever the image says. Refuse, before any server is
    # started, an image that is not linked for this family's RAM: a flash-linked
    # build would be programmed into flash on the run path (found by the
    # 2026-09-03 red-team, RT1) and then started from garbage.
    not_ram = image_mod.check_ram_image(elf, quirks.vector_table_addr)
    if not_ram:
        return failed(
            not_ram, probe_serial=serial, elf=str(elf),
            vector_table_addr=f"0x{quirks.vector_table_addr:08X}",
            vector_table_provenance=layout_provenance,
        )
    # This board's own console, not whichever ttyACM* happens to be first.
    port = probe_info.console_port if probe_info else console_mod.find_console_port()
    console_error: str | None = None
    gdb_server = None
    gdb_output = ""

    try:
        # Inside the try: running out of ports raises RunError, which used to
        # escape the tool as an unhandled exception instead of a result.
        gdb_port = _gdb_port_for(serial)
        # Family's port, not the one the programmer settled on: see
        # Probe.gdb_access_port for why those differ.
        gdb_server = acquire_server(
            toolchain, probe.gdb_access_port(info), gdb_port, serial=serial
        )
        _held[serial] = (probe, info, probe_info)

        if port:
            with console_mod.ConsoleCapture(port, _CONSOLE_BAUD) as cap:
                sp, pc, size, gdb_output = load_and_run(
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
            sp, pc, size, gdb_output = load_and_run(
                toolchain, elf, quirks.vector_table_addr,
                probe.gdb_access_port(info), gdb_port
            )
            text = ""
    except (RunError, ToolchainError, console_mod.ConsoleError) as exc:
        # Say whether the server started for this run is left holding the
        # probe. It used to be a bare error, and the next read_memory then
        # failed with "held" for a reason the agent had not been told.
        return failed(
            str(exc), probe_serial=serial,
            **_held_disclosure(serial), **_server_output(gdb_server),
        )

    detail: dict[str, Any] = {
        "probe_serial": serial,
        **_held_disclosure(serial),
        "console_port": port,
        "load_address": _hex(quirks.vector_table_addr),
        "load_address_provenance": layout_provenance,
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

    if layout_provenance != quirks_mod.MEASURED:
        detail["load_address_note"] = (
            f"The address this image was loaded to is {layout_provenance}, not "
            f"measured on hardware. The run itself IS verified, by this board's own "
            "console output, so the image did start. What is not established is that "
            f"{_hex(quirks.vector_table_addr)} is the right place for this part in "
            "general: a working run is good evidence for it and not a measurement of "
            "it. Do not record it as a confirmed layout on the strength of this "
            "result alone."
        )

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
        # GDB's own output is where the cause is, and it used to be dropped
        # here, the one path where it matters. Kept as a tail, since a batch
        # that failed early is short and one that ran long is mostly noise.
        return failed(
            "The GDB batch did not reach its end markers, so the load did not "
            "complete. Any console output here is the PREVIOUS image still "
            "running, not the one just requested. gdb_output says why.",
            gdb_output=gdb_output.strip().splitlines()[-20:],
            **_server_output(gdb_server),
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


def _held_disclosure(serial: str | None) -> dict[str, Any]:
    """Whether this server now holds the probe, for N3: the agent cannot see it."""
    held = server_is_held(serial)
    disclosure: dict[str, Any] = {"gdb_server_held": held}
    if held:
        disclosure["gdb_server_note"] = (
            "The GDB server started for run_firmware is holding this probe, so "
            "read_memory, core_registers, reset_target, read_fault_status and "
            "flash_firmware cannot reach it until release_probe()."
        )
    return disclosure


def _server_output(gdb_server: Any) -> dict[str, Any]:
    """The GDB server's last lines, when a server was started, for diagnosis."""
    if gdb_server is None:
        return {}
    return {"gdb_server_output": gdb_server.recent_output(20)}


@mcp.tool()
def release_probe(probe_serial: str | None = None) -> dict[str, Any]:
    """Shut down held GDB servers and free their probes; with no serial, all of them.

Needed before read_memory, core_registers or flash_firmware can reach a probe
after a run. Never done implicitly: on an STM32N6 running a stock FSBL image,
releasing is a one-way door until the board is power cycled. `unverified` if
nothing was held.
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
    """Program a raw binary image (.bin) into boot memory for standalone boot.

Prefer run_firmware while developing. Does not sign. Refuses anything but a
.bin, because the programmer writes HEX, S-record and ELF files to the
addresses inside them. Refuses a flash base that is not `measured` unless you
pass `address`. Nothing is read back afterwards; `verified_by` says what was
and was not checked.
    """
    # Checked before anything touches the probe, like run_firmware's image
    # check: a refusal after identify() would already have connected, and
    # possibly reset, for nothing.
    image = Path(image_path).expanduser()
    not_flashable = image_mod.check_flash_image(image)
    if not_flashable:
        return failed(not_flashable, image=str(image))
    image_size = image.stat().st_size

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
    try:
        info = probe.identify()
    except Exception as exc:  # noqa: BLE001
        return failed(str(exc), probe_serial=serial,
                      target_was_reset=probe.target_was_reset)
    if not info.connected:
        return failed(
            "No target",
            probe_serial=serial,
            target_was_reset=probe.target_was_reset,
            errors=info.errors,
            hint=explain_connect_failure(info),
        )

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
        output = probe.program_flash(str(image), target_address, quirks.external_loader)
    except Exception as exc:  # noqa: BLE001
        return failed(str(exc), probe_serial=serial,
                      target_was_reset=probe.target_was_reset)

    # The evidence names its own weakness deliberately. This is the one tool
    # whose only check is the vendor's own banner, which is exactly the thing
    # the README's opening example shows cannot be trusted on its own.
    return verified(
        "the programmer reported the download completed. The image was NOT read "
        "back and compared, so this confirms the write was accepted, not that "
        "flash holds what you sent",
        probe_serial=serial,
        image=str(image),
        image_size_bytes=image_size,
        address=_hex(target_address),
        target_was_reset=probe.target_was_reset,
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
    """Read target memory as 32-bit words without halting or resetting the target.

Connects hotplug-only. On a part whose running firmware has closed the debug
access port, reopening it would need a reset, which destroys the state you are
reading; this refuses instead and says why. Cannot reach a probe held by a GDB
server until `release_probe`.
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


def _default_boards_path() -> Path | None:
    """The default board file, or None if there is no home directory to look in.

    Deliberately a function. As a module-level constant this called Path.home()
    at IMPORT time, so in an environment where it raises - HOME unset and the UID
    absent from passwd, which some minimal container launches produce - the
    failure happened during `from .server import ...`, before main() installed
    the handler built to turn start-up problems into a one-line message and exit
    2. A raw traceback instead, from the one part of start-up that exists to
    prevent raw tracebacks.
    """
    try:
        return Path.home() / ".config" / "stlink-mcp" / "boards.toml"
    except RuntimeError:
        return None


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
    default = _default_boards_path()
    return default if default is not None and default.exists() else None


#: Set to any non-empty value to run on measured and user-stated data only.
_NO_SEED_ENV = "STLINK_MCP_NO_VENDOR_SEED"


def vendor_database_dir() -> Path:
    """Where the device databases live, inside whichever CubeCLT is selected."""
    return find_toolchain().programmer_bin_dir.parent / "Data_Base"


def load_vendor_seed() -> str:
    """Seed the quirks table from the installed device databases.

    Returns a one-line human-readable status, ALWAYS, and never an empty string.
    That is the fix for three different conditions that used to be indented into
    the same silence: the feature switched off, no CubeCLT found, and a parse that
    yielded nothing because ST renamed a tag. All three returned (0, 0) and the
    start-up message was gated on a non-zero count, so a format change that lost
    all 99 device IDs looked exactly like a machine with no toolchain.

    Failure to seed is NOT fatal, which is the one place this differs from the
    user's board file: nobody asked for these entries, so their absence returns
    the server to refusing on uncharacterised parts, which is where it was and is
    safe. A board file the user wrote is a statement about their hardware, and
    starting without it would answer questions about a different machine.
    """
    if os.environ.get(_NO_SEED_ENV):
        return f"vendor seeding is OFF because {_NO_SEED_ENV} is set"
    try:
        db_dir = vendor_database_dir()
    except ToolchainError as exc:
        return f"vendor seeding skipped, no STM32CubeCLT install found ({exc})"
    try:
        result = seed_from_directory(db_dir)
    except VendorDbError as exc:
        return f"vendor seeding skipped: {exc}"

    quirks_mod.set_vendor_boards(result.entries, result.refusals)

    parts = [f"seeded {len(result.entries)} device IDs from {db_dir}"]
    if result.refusals:
        parts.append(f"{len(result.refusals)} refused as ambiguous")
    if result.unreadable:
        # Loud, because a file that matched the glob and could not be read is the
        # one case here that means something is wrong rather than merely unknown.
        parts.append(
            f"{len(result.unreadable)} UNREADABLE: "
            + "; ".join(f"{n} ({r})" for n, r in sorted(result.unreadable.items()))
        )
    if not result.entries:
        parts.append(
            "WHICH IS NONE - the directory exists and yielded nothing, so the "
            "database format has probably changed and every part will now be "
            "treated as uncharacterised"
        )
    return ", ".join(parts) + ". None of this is measured; board_state reports the provenance of every value."


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
def reset_target(probe_serial: str | None = None) -> dict[str, Any]:
    """Reset the target and check that it actually restarted.

The reset is a resetting connect; the core is halted at the reset vector just
long enough to compare the PC against word 1 of the vector table, then
released, and that brief halt is reported. The result does not claim whether
the core is halted now, because nothing here can observe that. To hold a
target stopped, use the GDB server that run_firmware keeps alive.
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

    # Release the core BEFORE deciding the verdict, on every path where
    # something was halted. Whether the reset could be VERIFIED and whether the
    # core was released are independent, and the first version tied them
    # together: the release lived inside the verified branch, so an unverified
    # outcome skipped it.
    #
    # The release is kept even though a programmer halt does not outlive its own
    # invocation, so on the two H7 boards here the core resumes regardless. It
    # stays because that was measured on two parts and not on the others, and a
    # confirmed resume costs one connect while an assumed one costs the
    # guarantee.
    halted_to_read_pc = pc is not None
    released = False
    release_error: str | None = None
    if halted_to_read_pc:
        try:
            probe.release_core(access_port)
            released = True
        except Exception as exc:  # noqa: BLE001
            release_error = str(exc)

    detail: dict[str, Any] = {
        "probe_serial": serial,
        "family": quirks.name,
        "reset_kind": "hardware pulse on nRST (HWRSTPULSE connect)",
        "program_counter": _hex(pc),
        "expected_reset_handler": _hex(None if expected is None else expected & ~1),
        # What was DONE, not what the core is doing now. The difference is the
        # whole point: these two are observations, and "core_halted" was a guess
        # wearing a field name.
        "halted_to_read_pc": halted_to_read_pc,
        "core_released": released,
    }
    if released:
        detail["note"] = (
            "The core was halted only long enough to read the program counter, "
            "and the programmer confirmed it resumed. Note that a halt made this "
            "way does not outlive the command that made it in any case."
        )

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
            hint = (
                f"On {quirks.name} the debug access port is most likely to have "
                "been closed ALREADY, by an image that ran before this call, and "
                "a reset does not reopen it on this family. Only removing power "
                "does. This is not evidence that the reset failed, and it is not "
                "evidence that it worked."
            )
        else:
            # Reachable for every part this project has not characterised, since
            # debug_closes_on_run defaults to None. The first version put the
            # note only behind the truthy branch, which left the majority case
            # with a bare statement and no theory to act on.
            hint = (
                "Things to separate, in the order that costs least: is the probe "
                "held by another process (check by PORT with `ss -ltnp`, never by "
                "process name); is the target powered and out of reset; is the "
                "access port right for this part, which board_state reports along "
                "with where that number came from. A debug port that answered "
                "before the pulse and not after is worth a power cycle before it "
                "is worth suspecting the board."
            )
        return unverified(
            reason, hint=hint,
            detail_output=output.strip().splitlines()[-3:], **detail,
        )

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
            hint=(
                "Three things produce this, and they are distinguishable. The "
                "part may not be the family that was identified, so check "
                "board_state's device id. The image may relocate its vector "
                "table, in which case the handler in flash is not where it is "
                "executing from. Or the halt genuinely lost the race, which is "
                "what a plain `-rst` followed by `-halt` does and is why this "
                "tool resets by connecting instead."
            ),
            **detail,
        )

    # The reset itself is confirmed from here down. A release that was asked for
    # and did not happen is still a failure of what the caller requested: they
    # asked for a target that is reset and running, and this one is reset and
    # stopped. Reporting that inside a `verified` envelope with a note is the
    # shape result.py exists to stop, so the evidence for the reset is carried
    # in the payload and the verdict follows the state the board is actually in.
    if release_error is not None:
        return failed(
            "The reset was confirmed, but the programmer would not confirm the "
            f"core resumed: {release_error}",
            reset_verified_by=(
                f"the core halted at {_hex(pc)}, which is the reset handler named "
                f"by word 1 of the vector table at {_hex(vector_addr)}"
            ),
            hint=(
                "Nothing here needs a power cycle. The core may well be running "
                "anyway, since a halt made through the programmer does not "
                "outlive the command that made it, but that was not confirmed "
                "and so is not claimed. Retry reset_target."
            ),
            **detail,
        )

    return verified(
        f"the core halted at {_hex(pc)}, which is the reset handler named by "
        f"word 1 of the vector table at {_hex(vector_addr)}",
        **detail,
    )


@mcp.tool()
def core_registers(probe_serial: str | None = None) -> dict[str, Any]:
    """Read the core registers, halting the core for the read.

The direct way to find out what a program is doing, and what settles "did it
start" when there is no console. Does not claim the core is still halted
afterwards: a programmer halt did not outlive its command on the boards
measured. Reports `target_was_reset` when reaching the core needed a reset.
Perturbing, so never used inside routine checks.
    """
    probe: Probe | None = None
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
        # The connect is checked before the read. It used to be discarded, so a
        # target nothing could reach surfaced as a bare parse failure from the
        # register read, without the connect errors, the hint, or the fact that
        # identify() had pulsed reset trying to reach it.
        info = probe.identify()
        if not info.connected:
            return failed(
                "Could not reach the core.",
                probe_serial=serial,
                target_was_reset=probe.target_was_reset,
                errors=info.errors,
                hint=explain_connect_failure(info),
            )
        regs = probe.core_registers()
    except Exception as exc:  # noqa: BLE001
        return failed(
            str(exc), target_was_reset=bool(probe and probe.target_was_reset)
        )

    detail: dict[str, Any] = {
        "probe_serial": serial,
        # What was DONE, not a claim about the core now. This used to be
        # `"halted": True` beside "The core is now halted", the same unobserved
        # claim reset_target dropped on 2026-08-17 after two H7 boards showed a
        # programmer halt does not outlive the command that made it
        # (docs/findings.md, "A halt does not survive the invocation").
        "halted_to_read_registers": True,
        "connect_mode": probe.connect_mode,
        # Reported, not hidden. Reaching the family's access port on a part that
        # closes it can need a resetting connect, and a reset restarts the very
        # program being inspected. A caller cannot allow for that if it is not told.
        "target_was_reset": probe.target_was_reset,
        "registers": {name: f"0x{value:08X}" for name, value in regs.items()},
        "note": (
            "The core was halted to read these registers. Whether it is still "
            "halted is not claimed: a halt made through the programmer did not "
            "outlive the command on the boards measured, and nothing here can "
            "observe it. To hold a target stopped, use the GDB server that "
            "run_firmware keeps alive."
        ),
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
def read_fault_status(probe_serial: str | None = None) -> dict[str, Any]:
    """Read and decode the Cortex-M fault status registers without halting or
resetting the target.

The registers are sticky until software clears them, so call this after a
crash, or on a running target to check whether it faulted and recovered. The
result says whether a fault is active, what happened in plain English, the
faulting address when the hardware recorded it, and how to find the faulting
instruction. Refuses Cortex-M0, M0+, M1 and M23 cores, which lack these
registers.
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
                "Could not reach the core without resetting it. Reading the "
                "fault registers requires a debug connection, and resetting "
                "clears them, which would destroy the information this tool "
                "exists to read.",
                probe_serial=serial,
                hint=explain_connect_failure(info),
            )
        # Refused before any read. These cores take every fault as a HardFault
        # and record nothing in this region, so a read of it would decode as
        # "no fault active" for a core sitting in its HardFault handler: the
        # confidently wrong answer, on a whole class of parts the vendor seed
        # covers (C0, F0, G0, L0 and more).
        if faults_mod.has_fault_registers(info.cpu) is False:
            return failed(
                f"{info.cpu} has no configurable fault status registers. Armv6-M "
                "and Armv8-M Baseline cores take every fault as a HardFault and "
                "record nothing in CFSR, HFSR, MMFAR, BFAR or AFSR, so reading "
                "them cannot say whether this core faulted.",
                probe_serial=serial,
                cpu=info.cpu,
                hint=(
                    "core_registers() answers it instead, halting the core for "
                    "the read: XPSR bits 0-8 read 3 while the core is in its "
                    "HardFault handler, and 0 in thread mode."
                ),
            )
        words = probe.read_memory(
            faults_mod.FAULT_REGION_BASE, faults_mod.FAULT_REGION_SIZE,
        )
    except Exception as exc:  # noqa: BLE001
        return failed(str(exc))

    # A register the read did not return is not a clean register. decode()
    # treats an absent word as zero, and zero is "no fault bit set".
    missing = faults_mod.missing_registers(words)
    if missing:
        return failed(
            f"The read returned {len(faults_mod.REGISTERS) - len(missing)} of the "
            f"{len(faults_mod.REGISTERS)} fault registers; {', '.join(missing)} "
            "did not come back. A register that was not read is not reported "
            "as clean.",
            probe_serial=serial,
            words_read={f"0x{a:08X}": f"0x{v:08X}" for a, v in sorted(words.items())},
        )

    status = faults_mod.decode(words)
    registers_known = faults_mod.has_fault_registers(info.cpu)

    detail: dict[str, Any] = {
        "probe_serial": serial,
        "cpu": info.cpu,
        "connect_mode": probe.connect_mode,
        "target_was_reset": probe.target_was_reset,
        "any_fault_active": status.any_fault,
        "diagnosis": status.diagnosis,
        "faults": [
            {"category": f.category, "bit": f.name, "meaning": f.meaning}
            for f in status.faults
        ],
        "raw_registers": {
            name: f"0x{val:08X}" for name, val in status.raw.items()
        },
        "note": (
            "These registers are sticky: they hold the state from the most "
            "recent fault until software clears them by writing 1 to each set "
            "bit, or until the target is reset. A clean read means no fault "
            "since the last reset or clear, not that the program is healthy."
        ),
    }

    if status.mmfar is not None:
        detail["memmanage_fault_address"] = f"0x{status.mmfar:08X}"
    if status.bfar is not None:
        detail["bus_fault_address"] = f"0x{status.bfar:08X}"

    if status.debug:
        detail["debug_halt_reason"] = [
            {"bit": d.name, "meaning": d.meaning} for d in status.debug
        ]

    if faults_mod.may_bank_by_security(info.cpu):
        detail["security_state_note"] = (
            "If this core implements the Armv8-M Security Extension (TrustZone), "
            "MMFSR, UFSR and MMFAR are banked by security state and this read "
            "sees one bank, so a MemManage fault or UsageFault taken in the "
            "other security state may not appear here."
        )

    if status.any_fault:
        # The stack matters. The previous text said to read the frame at "the
        # SP", which inside a handler is MSP; a fault in thread code under any
        # RTOS stacks its frame on PSP, so following it gave a confident, wrong
        # faulting PC. EXC_RETURN in LR says which.
        detail["next_step"] = (
            "To find WHERE it happened, call core_registers(), which halts the "
            "core for the read. XPSR bits 0-8 give the active exception (3 is "
            "HardFault, 0 is thread mode). In a handler, LR holds an EXC_RETURN "
            "value (0xFFxxxxxx) whose bit 2 names the stack holding the "
            "exception frame: clear is MSP, which is the SP a handler reports; "
            "set is PSP, the thread stack an RTOS uses, which that SP is not. "
            "Read 32 bytes at the frame's stack pointer with read_memory: words "
            "0-3 are R0-R3, 4 is R12, 5 is LR, 6 is the faulting PC, 7 is XPSR. "
            "If LR bit 4 is clear the frame also holds FP state, 104 bytes in "
            "all. If LR is not 0xFFxxxxxx the handler has overwritten it, and "
            "the frame cannot be located this way."
        )

    if probe.degraded_reason:
        return unverified(probe.degraded_reason, **detail)

    # Set bits are evidence on any core. A clean read is evidence only on a
    # core known to have the registers, since elsewhere the region is not fault
    # status at all.
    if registers_known is None and not status.any_fault:
        return unverified(
            "The registers read clean, but the connect reported the core as "
            f"{info.cpu or 'nothing'}, which this tool does not recognise as "
            "having them. On a core without them (Cortex-M0, M0+, M1, M23) this "
            "region is not fault status, so a clean read there does not show "
            "that nothing faulted.",
            **detail,
        )

    return verified("fault status registers read from the target", **detail)


@mcp.tool()
def capture_console(
    seconds: float = 5.0, probe_serial: str | None = None
) -> dict[str, Any]:
    """Capture what a target prints on its serial console for a number of seconds.

The port is resolved from the probe's serial, never by taking the first
/dev/ttyACM*, which with two boards attached is the wrong one about half the
time. Refuses when it cannot resolve a port for that probe.
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
