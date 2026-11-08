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

import contextlib
import contextvars
import dataclasses
import difflib
import functools
import inspect
import os
import re
import sys
import threading
import time
import weakref
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Literal, TypeVar

from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from . import __version__
from . import console as console_mod
from . import debugger as debugger_mod
from . import disasm as disasm_mod
from . import gdbmi
from . import faults as faults_mod
from . import image as image_mod
from . import quirks as quirks_mod
from . import read_effects as read_effects_mod
from . import sampling as sampling_mod
from . import settings
from . import svd as svd_mod
from . import symbols as symbols_mod
from .result import failed, unverified, verified
from .probe import (
    CoreState,
    Probe,
    ProbeError,
    ProbeInfo,
    TargetInfo,
    explain_connect_failure,
    list_probes as _list_probes,
    list_probes_with_unreadable,
    parse_option_bytes,
    reset_landed_at_vector,
    reset_vector_address,
)
from .runner import (
    RunError,
    port_is_taken,
    acquire_server,
    held_serials,
    held_server,
    interface_mismatch,
    load_and_run,
    release_server,
    server_is_held,
)
from .toolchain import Toolchain, ToolchainError, find_toolchain, run_command, strip_ansi
from .vendordb import VendorDbError, seed_from_directory

# The version goes into the initialize handshake, so a client log or a bug
# report shows which build answered.
#: What every client is told on connecting, whether or not it loads the tools'
#: descriptions: Claude Code shows server instructions for tools it has
#: deferred, and Google's Antigravity command line gives its model the tools
#: only through one generic call.
INSTRUCTIONS = """\
stlink-mcp drives STM32 boards through an ST-LINK probe with ST's STM32CubeCLT tools.

Every result has a status. verified: verified_by says what was observed. unverified: it may have \
worked, but nothing checked it, and unverified_because says why; this is not success. failed: \
error says why. Read the status before the values.

A usual session: `list_probes`, then `board_state` to identify the board. `run_firmware` loads an \
ELF linked for RAM and starts it; a build linked for flash, the default from STM32CubeIDE and \
CubeMX, goes `flash_firmware`, then `reset_target`, then `capture_console`. Without halting the program: \
`capture_console`, `capture_swo`, `sample_values`, `read_memory`, `read_fault_status`, \
`read_option_bytes` and `disassemble`. Halting it: `run_until`, `core_registers` and \
`locate_fault`. `reset_target` restarts it; `release_probe` when done.

Give `probe_serial` on every call: without it each call first lists the probes, which takes a \
second or more and holds up every other board. Give addresses as "0x..." strings and files as \
absolute paths. `run_firmware` lets go of the probe once the program starts; with \
`keep_probe=True` a GDB server keeps holding it, and while it does, a core that locks up is reset, \
which clears its fault registers. `reset_target` and `flash_firmware` stop a holding server first \
and say so. A call that halts or resets the target says so. If the running program has closed the \
debug port, `board_state` can get back in by resetting the target, and `reset_target` cannot; on \
an STM32N6 running ST's start-up code, only a power cycle does. An unknown argument is refused, \
not ignored.
"""


class _Server(MCPServer):
    """An MCPServer that refuses an argument no parameter of the tool has.

    The SDK drops one without a word (MCP SDK 2.0.0, measured over stdio on
    2026-10-05): `lenght` left read_memory reading its default 32 bytes, and a
    misspelt `interface` would connect over SWD with nothing to say so, which
    N8 forbids. Agents guess parameter names when a client does not show them
    the schemas, as a model run through Google's Antigravity command line did."""

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Context | None = None) -> Any:
        tool = self._tool_manager.get_tool(name)
        known = set((tool.parameters or {}).get("properties", {})) if tool is not None else set()
        unknown = sorted(set(arguments) - known) if tool is not None else []
        if unknown:
            nearest = {u: near[0] for u in unknown if (near := difflib.get_close_matches(u, sorted(known), n=1))}
            result = failed(
                f"{name} has no parameter {', '.join(unknown)}"
                + (f" (did you mean {', '.join(f'{v} for {k}' for k, v in nearest.items())}?)" if nearest else "")
                + ". Nothing was done. Its parameters: " + ", ".join(sorted(known)) + ".",
                unknown_arguments=unknown,
            )
            _trace(_caller_from(context), name, {}, result)
            return tool.fn_metadata.convert_result(result)
        return await super().call_tool(name, arguments, context)


mcp = _Server("stlink-mcp", version=__version__, instructions=INSTRUCTIONS)

#: The debug interface a tool connects over (N8). A parameter, not an
#: assumption: SWD by default, since every probe that does JTAG also does SWD.
Interface = Literal["swd", "jtag"]


@dataclasses.dataclass(frozen=True)
class Caller:
    """Who asked for an operation (N9). Always the local user today; recorded so
    identity and per-caller policy can be added without a redesign."""

    client: str
    """The name and version the MCP client declared when it connected."""
    connection: int
    """Which connection to this server, counted from 1 in arrival order."""
    request: str
    """The MCP request id."""

    @property
    def label(self) -> str:
        return f"{self.client}, connection {self.connection}, request {self.request}"


#: A call made in-process rather than over MCP: tests, tools/hwcheck's builds.
_IN_PROCESS = Caller("in-process", 0, "-")
_CALLER: contextvars.ContextVar[Caller] = contextvars.ContextVar("caller", default=_IN_PROCESS)
_CONNECTIONS: weakref.WeakKeyDictionary[Any, int] = weakref.WeakKeyDictionary()
_CONNECTIONS_BY_ID: dict[int, int] = {}
_CONNECTION_COUNT = [0]


def _connection_number(session: Any) -> int:
    """A number per connection, in arrival order, keyed on an object that lives
    as long as the connection. Kept by weak reference where the object allows
    one, so a closed connection's number is not inherited."""
    with _STATE:
        try:
            known = _CONNECTIONS.get(session)
        except TypeError:
            known = _CONNECTIONS_BY_ID.get(id(session))
        if known is not None:
            return known
        _CONNECTION_COUNT[0] += 1
        try:
            _CONNECTIONS[session] = _CONNECTION_COUNT[0]
        except TypeError:
            _CONNECTIONS_BY_ID[id(session)] = _CONNECTION_COUNT[0]
        return _CONNECTION_COUNT[0]


def _caller_from(ctx: Context | None) -> Caller:
    if ctx is None:
        return _IN_PROCESS
    try:
        session = ctx.session
        params = session.client_params
        info = params.client_info if params is not None else None
        client = f"{info.name} {info.version}" if info is not None else "an unnamed client"
        # The session object is new on every request (measured 2026-10-01 with
        # MCP SDK 2.0); the client's initialize parameters are kept once per
        # connection, so they are what identifies one.
        return Caller(client, _connection_number(params if params is not None else session), ctx.request_id)
    except Exception:  # noqa: BLE001 - identity must never be why a tool fails
        return Caller("an unidentified client", 0, "-")


def current_caller() -> Caller:
    """The caller of the tool call running in this thread."""
    return _CALLER.get()


def _trace(caller: Caller, tool: str, args: dict[str, Any], result: dict[str, Any]) -> None:
    """One line on stderr per tool call, which MCP clients keep in their logs, so
    every operation can be traced to who asked for it (N9)."""
    shown = {k: v for k, v in args.items() if k in ("probe_serial", "interface", "address", "elf_path", "image_path") and v is not None}
    detail = ", ".join(f"{k}={v}" for k, v in shown.items())
    print(f"stlink-mcp: [{caller.label}] {tool}({detail}) -> {result.get('status')}",
          file=sys.stderr, flush=True)


class _Gate:
    """Shared for calls on one named probe, exclusive for anything that lists.

    Measured 2026-10-01 with three probes attached: programmer commands on two
    different probes at once ran without a failure (89 reads), while listing
    probes (`STM32_Programmer_CLI -l`) beside them broke 2 of 148 reads on
    another probe, one of them with the identity read back as nonsense. So a
    call that names its probe runs beside calls on other probes, and anything
    that lists waits until no probe is in use and holds the rest off.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._shared = 0
        self._exclusive: threading.Thread | None = None
        self._depth = 0

    @contextlib.contextmanager
    def shared(self) -> Iterator[None]:
        me = threading.current_thread()
        with self._cond:
            nested = self._exclusive is me  # already exclusive in this thread
            if nested:
                self._depth += 1
            else:
                while self._exclusive is not None:
                    self._cond.wait()
                self._shared += 1
        try:
            yield
        finally:
            with self._cond:
                if nested:
                    self._depth -= 1
                else:
                    self._shared -= 1
                self._cond.notify_all()

    @contextlib.contextmanager
    def exclusive(self) -> Iterator[None]:
        me = threading.current_thread()
        with self._cond:
            if self._exclusive is me:
                self._depth += 1
            else:
                while self._exclusive is not None or self._shared:
                    self._cond.wait()
                self._exclusive = me
                self._depth = 1
        try:
            yield
        finally:
            with self._cond:
                self._depth -= 1
                if self._depth == 0:
                    self._exclusive = None
                self._cond.notify_all()

    def held_exclusively_here(self) -> bool:
        return self._exclusive is threading.current_thread()


#: The MCP SDK runs each request as its own task and each synchronous tool in a
#: worker thread, so calls an agent issues together run together. Calls on
#: different probes may; calls on one probe queue behind each other, since two
#: programmer connects to one probe collide and two readers of one serial port
#: split its bytes; and a call that has to list the probes runs alone. A call
#: the client cancels still runs to completion in its thread, holding its locks
#: until it does. This is not per-caller state (N9): the probes are shared by
#: every caller there could be, so the locks belong to the hardware.
_GATE = _Gate()
_PROBE_LOCKS: dict[str, threading.RLock] = {}

#: Guards the server's own bookkeeping (GDB ports, held identities), which
#: calls on different probes touch at the same time.
_STATE = threading.RLock()

#: The tools that need a probe's serial console, and so its port.
_CONSOLE_TOOLS = frozenset({"board_state", "run_firmware", "capture_console"})

#: Where udev names each USB serial port after its device's serial number, as
#: usb-STMicroelectronics_STLINK-V3_<serial>-if02. Linux only.
_BY_ID_DIR = Path("/dev/serial/by-id")

_Tool = TypeVar("_Tool", bound=Callable[..., dict[str, Any]])


#: The access port each probe settled on last, so the next connect tries it
#: first (Probe.try_first). An order, not an answer: nothing is reported from it.
_SETTLED_PORTS: dict[str, int] = {}


def _settled_port(serial: str | None) -> int | None:
    with _STATE:
        return _SETTLED_PORTS.get((serial or "").upper())


def _remember_port(serial: str | None, probe: Probe, info: TargetInfo) -> None:
    if serial and info.connected and not probe.degraded_reason:
        with _STATE:
            _SETTLED_PORTS[serial.upper()] = probe.access_port


def _probe_lock(serial: str) -> threading.RLock:
    with _STATE:
        return _PROBE_LOCKS.setdefault(serial.upper(), threading.RLock())


def _listing_needed(probe_serial: str | None, console: bool) -> bool:
    """Whether resolving this probe has to list every probe.

    Not when the caller named the probe and the tool needs no console: the
    connect itself says when no such probe is attached. A tool that needs the
    console finds the port by the probe's serial in udev's names where those
    exist and no bare console path is set; a bare path applies only while one
    probe is attached, and that takes a count."""
    if probe_serial is None:
        return True
    if not console:
        return False
    return settings.console_is_bare_path()


#: Tool parameters that name a file on the machine running the server.
_FILE_ARGUMENTS = ("elf_path", "image_path")


def _serialised(tool: _Tool) -> _Tool:
    """Run a tool body under the gate and its probe's lock. Applied under
    @mcp.tool() on every tool, and TestToolCallsShareOnlyWhatTheyMust fails for
    one that lacks it."""
    signature = inspect.signature(tool)
    takes_serial = "probe_serial" in signature.parameters
    console = tool.__name__ in _CONSOLE_TOOLS

    @functools.wraps(tool)
    def gated(*args: Any, **kwargs: Any) -> dict[str, Any]:
        binding = signature.bind_partial(*args, **kwargs)
        bound = binding.arguments
        # One spelling per probe: what the server holds, and the GDB ports and
        # locks, are keyed by serial, and the programmer prints them upper case.
        if takes_serial and isinstance(bound.get("probe_serial"), str):
            bound["probe_serial"] = bound["probe_serial"].strip().upper()
        # A file is named the same way to every tool: "~" expanded, and a relative
        # path refused, since it would be looked up from this server's working
        # directory, which an MCP client need not set to the agent's project.
        refusal = None
        for name in _FILE_ARGUMENTS:
            value = bound.get(name)
            if isinstance(value, str) and value.strip():
                path = Path(value.strip()).expanduser()
                if not path.is_absolute():
                    refusal = failed(
                        f"{name} {value!r} is a relative path. This server looks files up from "
                        f"its own working directory, {Path.cwd()}, which need not be your "
                        f"project's (there it would be {Path.cwd() / path}). Give an absolute path."
                    )
                    break
                bound[name] = str(path)
        serial = bound.get("probe_serial") if takes_serial else None
        caller = _caller_from(bound.get("ctx"))
        token = _CALLER.set(caller)
        try:
            if refusal is not None:
                result = refusal
            elif serial is None or _listing_needed(serial, console):
                with _GATE.exclusive():
                    result = tool(*binding.args, **binding.kwargs)
            else:
                with _GATE.shared(), _probe_lock(serial):
                    result = tool(*binding.args, **binding.kwargs)
        except Exception as exc:  # noqa: BLE001
            # X1: an exception the tool did not turn into a result would reach
            # the agent with no status, and N4: what it did to the target first
            # is not known here.
            result = failed(
                f"The call failed with {type(exc).__name__}: {exc}",
                target_was_reset=None,
                reset_note=(
                    "This failure was not handled where it happened, so whether the "
                    "target was reset or halted first is not known."
                ),
            )
        finally:
            _CALLER.reset(token)
        _trace(caller, tool.__name__, bound, result)
        return result

    return gated  # type: ignore[return-value]


# One GDB server instance opens TWO ports: the one given with -p, and -p + 1 for
# SWO. Measured, not assumed: `ss -ltnp` against a running server shows both.
# With a stride of 1, the second board's GDB port lands exactly on the first
# board's SWO port, which is how a four-board rig fails on its second load.
# The base port and the console baud rate are settings.py's, read when used.
_GDB_PORT_STRIDE = 4

# Cached per probe while its GDB server is held. Re-identifying would mean
# running STM32_Programmer_CLI, which cannot reach a probe the server has.
_held: dict[str | None, tuple[Probe, TargetInfo, ProbeInfo | None]] = {}


#: Who acquired each held GDB server (N9): state keyed to a caller, so a refusal
#: can say who holds the probe and a release who held it.
_held_by: dict[str | None, Caller] = {}


def _held_by_disclosure(serial: str | None) -> dict[str, Any]:
    with _STATE:
        holder = _held_by.get(serial)
    return {"held_by": holder.label} if holder is not None else {}


def _cached_for(serial: str | None):
    """The cached identity, but only while a live server actually holds it.

    The cache exists because a server held without stlink-server blocks the
    programmer CLI. Once that server dies the reason is gone, so the entry must
    go too: otherwise the
    identity of a board nobody is holding keeps being served from memory long
    after it stopped being true.
    """
    with _STATE:
        if serial in _held and server_is_held(serial):
            return _held[serial]
        _held.pop(serial, None)
        return None


_HELD_MESSAGE = (
    "A GDB server is holding this probe without stlink-server, so the programmer "
    "cannot connect beside it. core_registers, run_until and capture_console work "
    "through it; release_probe() frees it, after which a program that closes the "
    "debug port leaves it closed until a reset or a power cycle."
)


def _toolchain() -> Toolchain:
    toolchain = find_toolchain()
    toolchain.check()
    return toolchain


def _resolve_probe(
    toolchain: Toolchain, probe_serial: str | None, *, console: bool = False
) -> tuple[str | None, ProbeInfo | None, str | None]:
    """Pick which probe to talk to. Returns (serial, info, error).

    A named probe is not looked up in a listing unless the tool needs its
    console and udev's names cannot give it (_listing_needed): the connect
    says when no such probe is attached, and listing has two costs measured on
    2026-10-01. It took 11.3 seconds while a GDB server held another probe,
    against 1.36 seconds otherwise, which was every slow call seen that day;
    and run beside a command on another probe it broke 2 of 148 of them. (On
    2026-08-15, with five probes and a server held, it took 1.5 seconds; what
    differs between the two days is not established.) A listing therefore runs
    only under the gate's exclusive side, and this checks that it does.
    """
    if not _listing_needed(probe_serial, console):
        assert probe_serial is not None
        # The programmer ignores an sn= it cannot read as hex and connects to
        # the first probe instead (Probe._run), so refuse such a serial here.
        if not _SERIAL_SHAPE.fullmatch(probe_serial):
            return None, None, (
                f"probe_serial {probe_serial!r} is not a probe serial: those are hex "
                "digits, as list_probes shows them."
            )
        info = ProbeInfo(serial=probe_serial)
        if console:
            port, note, source = _console_by_serial(probe_serial)
            info = dataclasses.replace(
                info, console_port=port,
                console_source=source if port else None, console_note=note,
            )
        try:
            # STLINK_MCP_CONSOLE pairs still win. The count matters only to a
            # bare path, which never reaches here (_listing_needed).
            return probe_serial, _with_console(info, 1), None
        except settings.SettingError as exc:
            return None, None, str(exc)

    if not _GATE.held_exclusively_here():
        raise RuntimeError(
            "Listing probes outside the gate's exclusive side, where it can break "
            "a command running on another probe. _listing_needed and _serialised "
            "disagree about this call."
        )
    attached = _list_probes(toolchain)
    if not attached:
        return None, None, "No ST-LINK probes found."

    chosen: ProbeInfo | None = None
    if probe_serial is None:
        if len(attached) > 1:
            listing = ", ".join(p.serial for p in attached)
            return None, None, (
                f"{len(attached)} probes are attached and no probe_serial was given. "
                f"Pass one of: {listing}. Not choosing for you, because probe order "
                "is not stable across replugs and using the wrong board is a "
                "failure that looks like success."
            )
        chosen = attached[0]
    else:
        chosen = next((p for p in attached if p.serial == probe_serial), None)
        if chosen is None:
            listing = ", ".join(p.serial for p in attached)
            return None, None, f"No probe with serial {probe_serial}. Attached: {listing}"

    try:
        return chosen.serial, _with_console(chosen, len(attached)), None
    except settings.SettingError as exc:
        return None, None, str(exc)


_SERIAL_SHAPE = re.compile(r"[0-9A-Fa-f]{8,32}")

#: console_source for a port found by the probe's serial in udev's names.
_UDEV = "udev serial name"
_USB_SERIAL = "USB serial number"


def _console_by_serial(serial: str) -> tuple[str | None, str | None, str]:
    """(port, note, source): the serial port that carries this probe's USB serial.

    udev's /dev/serial/by-id names where Linux has them. Elsewhere pyserial's
    port list, which on Windows reported the ST-LINK's serial for its COM port
    (COM3, with the STLINK-V3's own serial, 2026-10-02)."""
    if _BY_ID_DIR.is_dir():
        port, note = _udev_console(serial)
        return port, note, _UDEV
    try:
        from serial.tools import list_ports
        ports = sorted(p.device for p in list_ports.comports()
                       if (p.serial_number or "").upper() == serial.upper())
    except Exception:  # noqa: BLE001 - a port list that cannot be read finds nothing
        return None, None, _USB_SERIAL
    if len(ports) == 1:
        return ports[0], None, _USB_SERIAL
    if len(ports) > 1:
        return None, (
            f"Several serial ports carry probe {serial}'s serial ({', '.join(ports)}), so "
            f"none was chosen. Name one with {settings.CONSOLE}=<probe serial>=<port>."
        ), _USB_SERIAL
    return None, None, _USB_SERIAL


def _udev_console(serial: str) -> tuple[str | None, str | None]:
    """(port, note): the serial port udev names after this probe, if exactly one.

    The name carries the USB serial number, which for an ST-LINK is the serial
    the programmer reports (checked 2026-10-01 on an STLINK-V3E and an N6
    board's STLINK-V3), and it survives a replug that renumbers /dev/ttyACM*.
    The by-id path itself is returned, for that reason. A probe with no serial
    port, such as an ST-LINK/V2, has no entry."""
    wanted = f"_{serial.upper()}-IF"
    try:
        matches = sorted(p for p in _BY_ID_DIR.iterdir() if wanted in p.name.upper())
    except OSError:
        return None, None
    if len(matches) == 1:
        return str(matches[0]), None
    if len(matches) > 1:
        names = ", ".join(m.name for m in matches)
        return None, (
            f"Several serial ports carry probe {serial}'s serial ({names}), so none "
            f"was chosen. Name one with {settings.CONSOLE}=<probe serial>=<device path>."
        )
    return None, None


def _with_console(info: ProbeInfo, attached: int) -> ProbeInfo:
    """The probe with STLINK_MCP_CONSOLE applied, recording where its console
    came from. Every tool that uses a console reaches it through here, so the
    setting means the same thing in all of them."""
    device, note = settings.console_for(info.serial, attached)
    if device:
        info = dataclasses.replace(
            info, console_port=device, console_source=settings.CONSOLE
        )
    if note:
        info = dataclasses.replace(info, console_note=note)
    return info


def _console_evidence(probe_info: ProbeInfo | None) -> str:
    """Which port a console observation came from, for the evidence string."""
    if probe_info is not None and probe_info.console_source == settings.CONSOLE:
        return f"the serial port {settings.CONSOLE} assigns to this probe"
    return "this board's own port"


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
    with _STATE:
        return _allocate_gdb_port(serial)


def _allocate_gdb_port(serial: str | None) -> int:
    if serial in _gdb_ports:
        return _gdb_ports[serial]

    taken = set(_gdb_ports.values())
    base = settings.gdb_port_base()
    port = base
    ceiling = base + _GDB_PORT_STRIDE * 64
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
        f"No free GDB port pair between {base} and {ceiling}. Something "
        "is holding a great many of them: `ss -ltnp` will say what. "
        "STLINK_MCP_GDB_PORT moves the base."
    )


def _address(value: int | str, name: str = "address") -> int:
    """An address from a JSON integer or from a string such as "0x20000000".

    Strings are accepted because JSON has no hex literals. Without them an agent
    reading an address off a datasheet converts it to decimal in its head, and
    a slip there reads or writes somewhere else while the result looks fine.
    """
    if isinstance(value, bool):
        raise ValueError(f"{name} {value!r} is not an address.")
    if isinstance(value, str):
        try:
            number = int(value.strip(), 0)
        except ValueError:
            raise ValueError(
                f"{name} {value!r} is not a number this tool reads. Write hex with "
                "a 0x prefix, as in '0x20000000'. Digits alone are read as decimal, "
                "and digits with a leading zero are refused, since they are most "
                "likely hex without its prefix."
            ) from None
    else:
        number = value
    if number < 0:
        raise ValueError(f"{name} {number} is negative.")
    if number > 0xFFFFFFFF:
        raise ValueError(f"{name} 0x{number:X} is beyond the 32-bit address space.")
    return number


#: The most read_memory returns in one call. A result goes into the calling
#: agent's context whole, and a thousand-word dump there buries the few words
#: anyone needed; several reads are cheap.
_MAX_READ_BYTES = 1024

#: The longest console capture. A capture holds up every other call on its
#: probe until it ends, and every call on every probe when it names no probe.
_MAX_CAPTURE_SECONDS = 60.0


def _capture_seconds_problem(seconds: float, name: str) -> str | None:
    if 0 < seconds <= _MAX_CAPTURE_SECONDS:
        return None
    return (
        f"{name} is {seconds}. Give more than 0 and at most "
        f"{_MAX_CAPTURE_SECONDS:g}: a capture holds up every other call on its "
        "probe until it ends, and every call on every probe when no probe_serial "
        "is given."
    )


@mcp.tool()
@_serialised
def list_probes(
    ctx: Context | None = None,
) -> dict[str, Any]:
    """List every attached ST-LINK with its firmware, board name, serial console and
whether a GDB server holds it. Use the console mapping rather than the order of
/dev/ttyACM*, which does not follow the probes. Listing takes about a second,
and often much longer while a GDB server holds a probe, which is why the other
tools should be given `probe_serial`.
    """
    try:
        toolchain = _toolchain()
    except ToolchainError as exc:
        return failed(str(exc))

    held = set(held_serials())
    try:
        probes, unreadable, errors = list_probes_with_unreadable(toolchain)
        # Listed as the tools will use them, so a console setting shows here.
        probes = [_with_console(p, len(probes)) for p in probes]
    except (ToolchainError, settings.SettingError) as exc:
        return failed(str(exc))
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
    if not probes and errors:
        return unverified(
            "The programmer printed errors and listed no probe, so this does not "
            "show that none is attached: a probe this user cannot open prints an "
            "error and no entry.",
            errors=errors,
            hint=(
                "On Linux, check ST's udev rules (STM32CubeCLT installs them) and "
                "replug the probe, and `lsusb` for devices with vendor ID 0483."
            ),
            **detail,
        )
    if errors:
        # LP4: a probe this user cannot open prints an error and no entry, so
        # the list may be one short.
        detail["errors"] = errors
        detail["errors_note"] = (
            "The programmer also printed errors. A probe this user cannot open "
            "prints an error and no entry, so one may be missing from this list."
        )
    if not probes:
        detail["hint"] = (
            "If a board is plugged in, its probe is not reaching this machine. "
            "`lsusb` lists ST probes under vendor ID 0483; when none is there the "
            "cause is below this tool, in the cable, a hub or the USB controller."
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
                "console_port_source": p.console_source,
                **({"console_note": p.console_note} if p.console_note else {}),
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
@_serialised
def board_state(
    probe_serial: str | None = None,
    interface: Interface = "swd",
    elf_path: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Identify the target, the probe and its family, and say whether the core is
running, sleeping, halted or locked up, from program-counter samples read
without halting it; given `elf_path`, where in the source they fall. If the
core cannot be reached as it runs, reconnects through a reset, which restarts
the program and can get past a debug port the program closed; `target_was_reset` says
so. Never resets beside a GDB server holding the probe. `family.provenance`
says which values were measured.
    """
    # Checked before the connect, which may reset: a missing ELF used to come back
    # as program-counter places with no function, file or line, and no error.
    if elf_path and not Path(elf_path).exists():
        return failed(f"No such ELF: {elf_path}")
    try:
        toolchain = _toolchain()
    except ToolchainError as exc:
        return failed(str(exc))

    serial, probe_info, error = _resolve_probe(toolchain, probe_serial, console=True)
    if error:
        return failed(error)

    entry = _cached_for(serial)
    cached = entry is not None
    if entry is not None and _shares(serial):
        # Read live beside the held server, without a reset, rather than from
        # the identity captured when it was started.
        held_probe, _, probe_info = entry
        probe = Probe(toolchain, access_port=held_probe.access_port, serial=serial,
                      interface=interface, shared=True)
        info = probe.identify(allow_reset=False)
        cached = False
    elif entry is not None:
        probe, info, probe_info = entry
    else:
        probe = Probe(toolchain, serial=serial, interface=interface, try_first=_settled_port(serial))
        info = probe.identify()
        _remember_port(serial, probe, info)

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
        **_connection_fields(probe, info),
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
            # What changes what the agent does; the maintainers' history stays in
            # quirks.py. A board file's own notes stand in for it.
            "note": quirks.agent_note if quirks.agent_note is not None else quirks.notes,
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
        if probe.other_ports_that_answered:
            state["other_access_ports_that_answered"] = probe.other_ports_that_answered

    if not info.connected:
        return failed(
            "Could not reach the core.",
            errors=info.errors,
            hint=explain_connect_failure(info, interface),
            **state,
        )

    with _STATE:
        stopped = _stopped.get(serial)
    if not cached:
        state["core"] = _core_report(toolchain, probe, elf_path)
        mode, _ = _boot_mode(probe, quirks)
        if mode:
            state["boot_mode"] = mode
    elif stopped:
        state["core"] = {"state": "halted", "by": "run_until", "at": stopped}
    else:
        state["core"] = {"state": "not read", "why": "a GDB server holds the probe"}

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
@_serialised
def run_firmware(
    elf_path: str,
    probe_serial: str | None = None,
    console_seconds: float = 3.0,
    interface: Interface = "swd",
    keep_probe: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Reset the target, load an ELF into its RAM, start it, and verify that it
started: by its board's console output, or else by program-counter samples
inside the image. Refuses an image not linked for RAM: vector table at
board_state's `vector_table_addr`, every segment within 0x20000000-0x3FFFFFFF.
Lets go of the probe once the program starts; `keep_probe=True` keeps a GDB
server holding it, and a core that then locks up is reset, clearing its fault
registers. No signing.
    """
    too_long = _capture_seconds_problem(console_seconds, "console_seconds")
    if too_long:
        return failed(too_long)
    # Checked before the connect, which may reset the target to reach it.
    elf = Path(elf_path).expanduser()
    if not elf.exists():
        return failed(f"No such ELF: {elf}")
    try:
        toolchain = _toolchain()
    except ToolchainError as exc:
        return failed(str(exc))

    serial, probe_info, error = _resolve_probe(toolchain, probe_serial, console=True)
    if error:
        return failed(error)

    entry = _cached_for(serial)
    if entry is not None:
        probe, info, probe_info = entry
    else:
        probe = Probe(toolchain, serial=serial, interface=interface, try_first=_settled_port(serial))
        info = probe.identify()
        _remember_port(serial, probe, info)
        if not info.connected:
            return failed(
                "No target",
                probe_serial=serial,
                target_was_reset=probe.target_was_reset,
                errors=info.errors,
                **_connection_fields(probe, info),
                hint=explain_connect_failure(info, interface),
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
            "the value came from rather than guessing.",
            probe_serial=serial, target_was_reset=probe.target_was_reset,
        )
    # A GDB load writes wherever the image says. Refuse, before any server is
    # started, an image that is not linked for this family's RAM: a flash-linked
    # build would be programmed into flash on the run path (found by the
    # 2026-09-03 red-team, RT1) and then started from garbage.
    not_ram = image_mod.check_ram_image(elf, quirks.vector_table_addr)
    if not_ram:
        return failed(
            not_ram, probe_serial=serial, elf_path=str(elf), target_was_reset=probe.target_was_reset,
            vector_table_addr=f"0x{quirks.vector_table_addr:08X}",
            vector_table_provenance=layout_provenance,
        )
    # This board's own console, not whichever ttyACM* happens to be first.
    port = probe_info.console_port if probe_info else None
    console_error: str | None = None
    buffered = ""
    gdb_server = None
    gdb_output = ""
    stage = "before the server"

    try:
        # Inside the try: running out of ports raises RunError, which used to
        # escape the tool as an unhandled exception instead of a result.
        gdb_port = _gdb_port_for(serial)
        reused = held_server(serial) is not None
        # Family's port, not the one the programmer settled on: see
        # Probe.gdb_access_port for why those differ.
        stage = "starting the server"
        gdb_server = acquire_server(
            toolchain, probe.gdb_access_port(info), gdb_port, serial=serial,
            interface=interface,
        )
        stage = "held server" if reused else "new server"
        if reused and port:
            # A new server halts the running program as it starts, before the
            # console opens; a held one does not, and the previous image then
            # printed between the open and the load's reset, which verified an
            # image that prints nothing (H7, 2026-10-04). A GDB connect halts it.
            try:
                gdbmi.Session(toolchain.gdb, None, gdb_server.port).close(detach=False)
            except (gdbmi.MiError, OSError) as exc:
                raise RunError(f"Could not stop the previous program through the held GDB server "
                               f"before the console opened: {exc}") from exc
        with _STATE:
            _held[serial] = (probe, info, probe_info)
            _held_by.setdefault(serial, current_caller())
            _stopped.pop(serial, None)

        if port:
            with console_mod.ConsoleCapture(port, settings.console_baud()) as cap:
                # Set aside before the load: the probe's backlog of what the
                # previous image printed, which must not verify this one.
                buffered = cap.buffered
                sp, pc, size, gdb_output = load_and_run(
                    toolchain, elf, quirks.vector_table_addr,
                    probe.gdb_access_port(info), gdb_port
                )
                if not keep_probe:
                    _stop_holder_first(serial)
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
            if not keep_probe:
                _stop_holder_first(serial)
            text = ""
    except (RunError, ToolchainError, console_mod.ConsoleError,
            settings.SettingError) as exc:
        # Say whether the server started for this run is left holding the
        # probe. It used to be a bare error, and the next read_memory then
        # failed with "held" for a reason the agent had not been told.
        return failed(
            str(exc), probe_serial=serial, **_reset_before_a_failed_run(probe, stage),
            **_held_disclosure(serial), **_server_output(gdb_server),
        )

    # By default the probe is let go once the program starts. Holding it from
    # 108d2f8 was meant to keep a program that closes the debug port from locking
    # the debugger out; it did not: a program that takes the SWD pins cut off a
    # held server on the H7, and the STM32N6's usual start-up code closed its
    # port under a held server (2026-10-07). Holding costs a reset of a core that
    # locks up, which clears its fault registers (H7, F407 and N6, 2026-10-04),
    # and stuck probes followed shared-mode holding on two boards. keep_probe
    # keeps it held, for run_until and reads without a new GDB server.

    detail: dict[str, Any] = {
        "probe_serial": serial,
        "elf_path": str(elf),
        **_connection_fields(probe, info),
        **_held_disclosure(serial),
        **({"warning": _CLOSES_WARNING} if quirks.debug_closes_on_run else {}),
        "console_port": port,
        "console_port_source": probe_info.console_source if probe_info else None,
        **({"console_note": probe_info.console_note}
           if probe_info and probe_info.console_note else {}),
        "load_address": _hex(quirks.vector_table_addr),
        "load_address_provenance": layout_provenance,
        "image_initial_sp": _hex(sp),
        "image_entry_point": _hex(pc),
        "load_size_bytes": size,
        # The GDB batch resets the target before it loads, so a batch that
        # reached its end markers reset it; one that stopped early may not have.
        "target_was_reset": True if sp is not None and pc is not None else None,
        "console": text,
        **_garbled_note(text),
        **_buffered_disclosure(buffered, before="this run started", source=(
            "so it came from whatever ran before, not from this image, and it is "
            "not counted as evidence")),
    }

    if layout_provenance != quirks_mod.MEASURED:
        # Written before the verdict is known, so it claims nothing about the
        # run itself. It used to say "The run itself IS verified" on every run,
        # and on 2026-10-01 an STM32F407 run with no console, unverified, carried
        # that sentence: the first seeded part ever run on a board.
        detail["load_address_note"] = (
            f"The load address is {layout_provenance}, not measured on a board. A run "
            "verified here is evidence for it, not a measurement: do not record it as "
            "this part's confirmed layout."
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
            reset_note=(
                "The batch resets the target before it loads, and it stopped before "
                "its end, so whether that reset ran is not known."
            ),
            gdb_output=gdb_output.strip().splitlines()[-20:],
            **_server_output(gdb_server),
            **detail,
        )

    # Without console text, where the core executes says whether the image
    # started: program-counter samples read without halting it, which lay inside
    # the running image on the H7, the F407 and the N6 (2026-10-03). Read beside
    # the server, so only when it shares the probe.
    sampled: str | None = None
    if not text.strip() and (not keep_probe or (gdb_server is not None and gdb_server.shared)):
        inside, core = _samples_in_image(
            Probe(toolchain, access_port=probe.access_port, serial=serial, interface=interface,
                  shared=keep_probe), elf)
        detail.update(core)
        if inside:
            sampled = (f"the core is executing this image's code: {core['core']['samples_inside_image']} "
                       "program-counter samples, read without halting it, lie inside the image's "
                       "loaded segments")
    elif not text.strip():
        detail["core_not_sampled"] = (
            "stlink-server is not installed, so the GDB server keeps the probe to itself "
            "and nothing samples the program counter beside it; keep_probe=False would."
        )

    if console_error is not None:
        detail["console_error"] = console_error
        if text.strip():
            return verified(
                f"partial console output from {_console_evidence(probe_info)}, "
                "before the capture was interrupted",
                **detail,
            )
        if sampled:
            return verified(sampled, **detail)
        return unverified(
            f"The load completed, but the console could not be read: "
            f"{console_error}. Nothing observed whether the image started."
            + _sample_reason(detail),
            **detail,
        )

    if not port:
        if sampled:
            return verified(sampled, **detail)
        return unverified(
            "No serial console is mapped to this probe, so nothing observed "
            "whether the image started. The load itself completed. Name one with "
            f"{settings.CONSOLE}=<probe serial>=<device path>." + _sample_reason(detail),
            **detail,
        )

    if not text.strip():
        if sampled:
            return verified(sampled, **detail)
        return unverified(
            "The image loaded and was started, but the target printed nothing in "
            "the capture window. It may not be running, or it may not print."
            + _sample_reason(detail),
            **detail,
        )

    return verified(f"console output from {_console_evidence(probe_info)}", **detail)


def _garbled_note(text: str) -> dict[str, Any]:
    """A note when most of the console output is not text.

    Measured 2026-10-01: an H7 image printing at 57600 baud, read at the
    default 115200, verified its run on output that was all replacement and
    stray characters. The verdict is right, since bytes did arrive after the
    new image started, but the agent cannot read them and is not told why."""
    if len(text) < 16:
        return {}
    unreadable = sum(1 for ch in text if not (ch.isascii() and (ch.isprintable() or ch in "\r\n\t")))
    if unreadable * 2 <= len(text):
        return {}
    return {"console_garbled_note": (
        f"{unreadable} of {len(text)} characters are not readable text, which is "
        "what a wrong baud rate looks like. The console is read at "
        f"{settings.console_baud()} baud; {settings.CONSOLE_BAUD} sets it."
    )}


#: The most of a probe's backlog a result carries. An STLINK-V3E held about 8 KB:
#: the first 6 KB printed after the port closed and the last 2 KB before it
#: opened, with everything between dropped (docs/findings.md, 2026-10-01).
_BUFFERED_TAIL = 1000


def _buffered_disclosure(text: str, *, before: str, source: str) -> dict[str, Any]:
    """The output the probe handed over when the port opened, kept apart (N3)."""
    if not text:
        return {}
    size = len(text.encode("utf-8", errors="replace"))
    return {
        "console_buffered": text[-_BUFFERED_TAIL:],
        "console_buffered_bytes": size,
        "console_buffered_note": (
            f"Arrived when the port opened, before {before}: the probe keeps a few KB of "
            f"what the target printed while the port was closed, and when there was more, "
            f"the part in the middle is missing; {source}."
            + (f" The last {_BUFFERED_TAIL} characters are shown."
               if len(text) > _BUFFERED_TAIL else "")
        ),
    }


def _core_report(toolchain: Toolchain, probe: Probe, elf_path: str | None) -> dict[str, Any]:
    """The core's state and program-counter samples, read without halting it."""
    try:
        core = probe.core_state()
    except ProbeError as exc:
        return {"state": "not read", "why": str(exc).splitlines()[0]}
    report: dict[str, Any] = {"state": core.state, "dhcsr": f"0x{core.dhcsr:08X}"}
    # DFSR only as the evidence for that note: shown on every call it read as an
    # unexplained register beside read_fault_status's account of the same bits.
    if core.state == "halted" and core.vector_catch and probe.shared:
        report["dfsr"] = f"0x{core.dfsr:08X}"
        report["state_note"] = _VECTOR_CATCH_NOTE
    if core.state == "not seen running":
        report["state_note"] = ("No instruction retired between two reads of DHCSR, and neither the halt "
                                "nor the lockup bit is set. An STM32H743 read this way after a reset into "
                                "its own firmware.")
    if not core.sampler_on:
        report["pc_samples_note"] = ("Not sampled: the sampler is off (DEMCR.TRCENA clear) until a debugger "
                                     "turns tracing on, as run_firmware's and run_until's sessions do.")
    elif core.state in ("running", "locked up"):
        report["pc_samples"] = [f"0x{pc:08X}" for pc in core.pc_samples]
        if elf_path:
            report["pc_places"] = [_symbol(toolchain, Path(elf_path), pc & ~1)
                                   for pc in dict.fromkeys(core.pc_samples)]
    return report


def _sample_reason(detail: dict[str, Any]) -> str:
    """What the program-counter samples showed, for an unverified run."""
    core = detail.get("core")
    if not core:
        why = f" Its program counter was not sampled: {detail['core_not_sampled']}" if "core_not_sampled" in detail else ""
        return why + " core_registers() shows where the program counter is."
    if core["state"] == "not read":
        return (f" Its program counter could not be sampled: {core['why']} "
                "core_registers() reads it by halting the core.")
    if core["state"] != "running":
        return f" The core is {core['state']} (DHCSR {core['dhcsr']}); read_fault_status says whether it faulted."
    return (f" Only {core['samples_inside_image']} program-counter samples lie inside the image, so the "
            "core is executing other code.")


def _samples_in_image(probe: Probe, elf: Path) -> tuple[bool, dict[str, Any]]:
    """Whether the core runs this image's code, from program-counter samples
    read without halting it, and the samples as a result field."""
    try:
        core = probe.core_state()
    except ProbeError as exc:
        return False, {"core": {"state": "not read", "why": str(exc).splitlines()[0]}}
    try:
        segments = image_mod.load_segments(elf.read_bytes())
    except (OSError, image_mod.ImageError):
        segments = []
    inside = [any(seg.vaddr <= (pc & ~1) < seg.vaddr + seg.memsz for seg in segments) for pc in core.pc_samples]
    report = {"state": core.state, "dhcsr": f"0x{core.dhcsr:08X}",
              "pc_samples": [f"0x{pc:08X}" for pc in core.pc_samples],
              "samples_inside_image": f"{sum(inside)} of {len(inside)}"}
    ok = core.state == "running" and core.sampler_on and bool(inside) and all(inside)
    return ok, {"core": report}


def _held_disclosure(serial: str | None) -> dict[str, Any]:
    """Whether this server now holds the probe, for N3: the agent cannot see it."""
    server = held_server(serial)
    disclosure: dict[str, Any] = {"gdb_server_held": server is not None}
    if server is not None and not server.shared:
        disclosure["gdb_server_note"] = (
            "stlink-server is not installed, so this server keeps the probe to itself: "
            "read_memory, sample_values, read_fault_status, locate_fault, "
            "read_option_bytes, capture_swo, and disassemble given a probe, refuse "
            "until release_probe(). Installing STM32CubeCLT puts stlink-server on the PATH."
        )
    return disclosure


_CLOSES_WARNING = (
    "On an STM32N6, an image built on ST's usual start-up code closes the debug port "
    "until a power cycle, whether or not the probe is held. board_state's family note "
    "says which line does it."
)

#: For a core halted by a vector catch while a server held its probe.
_VECTOR_CATCH_NOTE = (
    "DFSR records a vector catch while a GDB server held the probe. On the H7, the F407 and "
    "the N6, a core that locked up (a fault inside its fault handler) was reset by the held "
    "server and halted at its reset vector, which clears the fault registers. To see such a "
    "fault, run again with run_firmware(keep_probe=False)."
)


def _shares(serial: str | None) -> bool:
    """Whether a server holds this probe in ST's shared mode, so the programmer
    can connect beside it with `shared`."""
    server = held_server(serial)
    return server is not None and server.shared


def _held_refusal(serial: str | None) -> dict[str, Any] | None:
    """A refusal when a server keeps this probe to itself; None when nothing
    holds it or the holder shares it."""
    if server_is_held(serial) and not _shares(serial):
        return failed(_HELD_MESSAGE, probe_serial=serial, held_serials=held_serials(),
                      **_held_by_disclosure(serial))
    return None


def _stop_holder_first(serial: str | None) -> dict[str, Any]:
    """Stop a server holding this probe before a reset or a flash write: either
    made beside it broke its GDB session (2026-10-03). The result says so."""
    if not server_is_held(serial):
        return {}
    with _STATE:
        _stopped.pop(serial, None)
        _held.pop(serial, None)
        _held_by.pop(serial, None)
    release_server(serial)
    return {"released_probe_first": True}


def _boot_mode(probe: Probe, quirks: FamilyQuirks) -> tuple[str | None, str | None]:
    """BS7: "development" or "flash" from the part's boot pins, or None and why
    not. Read without halting; the status register reads only while its clock
    runs, so a stopped clock is an unknown, not flash boot."""
    if quirks.boot_pins is None:
        return None, None
    clock_register, clock_bit, pins_register, development_bits = quirks.boot_pins
    try:
        words = probe.read_ranges([(clock_register, 4), (pins_register, 4)])
    except Exception as exc:  # noqa: BLE001
        return None, f"the boot pins could not be read: {exc}"
    if not words.get(clock_register, 0) & clock_bit:
        return None, "the boot pin register's clock is off"
    pins = words.get(pins_register, 0)
    return ("development" if pins & development_bits == development_bits else "flash"), None


def _reset_before_a_failed_run(probe: Probe, stage: str) -> dict[str, Any]:
    """N4 for a run that failed once its GDB server was asked for: a new server
    resets and halts the target as it starts, and with a held one the program
    is halted and the load resets it, neither of which the agent can see."""
    if stage == "new server":
        return {"target_was_reset": True, "halted_during_call": True, "reset_note": (
            "The GDB server started for this run reset and halted the target as it "
            "started; core_registers says where the core is now.")}
    if stage in ("starting the server", "held server"):
        return {"target_was_reset": True if probe.target_was_reset else None, "reset_note": (
            "This failed after the run began to use a GDB server, which halts the "
            "target and resets it before the load; whether it got that far is not known.")}
    return {"target_was_reset": probe.target_was_reset}


def _after_attach(was_halted: bool) -> dict[str, Any]:
    """N4 for a run_until refused after GDB attached, which halts the core."""
    return {"halted_during_call": True,
            "core_after": "halted, where an earlier stop left it" if was_halted else _DETACH_RESUMED}


def _server_output(gdb_server: Any) -> dict[str, Any]:
    """The GDB server's last lines, when a server was started, for diagnosis."""
    if gdb_server is None:
        return {}
    return {"gdb_server_output": gdb_server.recent_output(20)}


@mcp.tool()
@_serialised
def release_probe(
    probe_serial: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Stop the GDB server holding a probe; with no serial, every probe's. Resumes a
core run_until left halted. While held, the reading tools work beside the
server, but a core that locks up is reset, clearing its fault registers. A
program that closes the debug port closes it held or not; board_state then gets
back in through a reset, restarting the program, except on an STM32N6 running
ST's start-up code, which needs a power cycle. `unverified` if nothing was held.
    """
    with _STATE:
        affected = [probe_serial] if probe_serial is not None else list(_held)
        quirks = [_held[s][1].quirks for s in affected if s in _held]
        stops = {s: _stopped.pop(s) for s in affected if s in _stopped}

    # N8: the core is read afterwards over the interface the run used.
    interfaces = {s: getattr(held_server(s), "interface", "swd") for s in stops}
    for serial in stops:
        _resume_through_gdb(serial)
    released = release_server(probe_serial)
    with _STATE:
        holders = {s: _held_by.pop(s).label for s in affected if s in _held_by}
        for s in affected:
            _held.pop(s, None)

    detail: dict[str, Any] = {"released": released, "probes": affected}
    if holders:
        detail["held_by"] = holders
    if released and stops:
        detail["core_after"] = {s: _core_state_after_release(s, stop, interfaces[s]) for s, stop in stops.items()}
    if released and any(q.debug_closes_on_run for q in quirks):
        detail["warning"] = _CLOSES_WARNING

    if not released:
        return unverified(
            "No GDB server was held for that probe, so nothing was released. "
            "Either it was already free, or a different serial is holding it.",
            **detail,
        )

    return verified("gdb server shut down and the probe freed", **detail)


def _resume_through_gdb(serial: str | None) -> None:
    """Resume a core run_until left halted, by GDB's detach, before its server
    stops. Stopping a server in ST's shared mode left the F407's core halted two
    times in five, where without it the core ran again five times in five
    (2026-10-04); a detach resumed it on all three boards. A failure here is
    not raised: the DHCSR read after the release says whether the core runs."""
    server = held_server(serial)
    if server is None:
        return
    try:
        gdbmi.Session(_toolchain().gdb, None, server.port).close(detach=True)
    except Exception:  # noqa: BLE001
        pass


def _core_state_after_release(serial: str | None, stop: dict[str, Any], interface: str = "swd") -> str:
    """Whether a core run_until left halted runs again once its server is gone,
    from DHCSR's S_HALT bit, read without a reset. Stopping the server resumed
    it on the H7, the F407 and the N6 (measured 2026-10-02); this observes it."""
    where = f"halted by run_until at {stop.get('address')} ({stop.get('function') or 'unknown function'})"
    try:
        probe = Probe(_toolchain(), serial=serial, interface=interface, try_first=_settled_port(serial))
        info = probe.identify(allow_reset=False)
        if not info.connected:
            return f"{where}; whether it runs now could not be read without a reset"
        dhcsr = probe.read_memory(_DHCSR, 4).get(_DHCSR)
    except Exception as exc:  # noqa: BLE001
        return f"{where}; whether it runs now could not be read: {exc}"
    if dhcsr is None:
        return f"{where}; whether it runs now could not be read"
    if dhcsr & _S_HALT:
        return f"{where}, and still halted: DHCSR reads 0x{dhcsr:08X}"
    return f"{where}, and running again: DHCSR reads 0x{dhcsr:08X}, S_HALT clear"


_DHCSR, _S_HALT = 0xE000EDF0, 1 << 17


@mcp.tool()
@_serialised
def flash_firmware(
    image_path: str,
    probe_serial: str | None = None,
    address: int | str | None = None,
    interface: Interface = "swd",
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Program an image into flash for standalone boot: a raw .bin at `address`, or
at the part's flash base, or an ELF or Intel HEX file at the addresses inside
it, which must lie in the part's flash. Without `address` the flash base must
be measured (board_state's `family.provenance`). Verified only when every byte
written reads back the same. Does not sign or start the image; `next_step` says how it boots. Prefer run_firmware
while developing.
    """
    released: dict[str, Any] = {}
    return {**_flash_firmware(image_path, probe_serial, address, interface, released), **released}


#: What a write that stopped partway leaves, said with its failure.
_FLASH_MAY_HAVE_CHANGED = (
    "unknown: the programmer may have erased or written part of the range before it "
    "stopped, and nothing puts that back; write the image again"
)


def _flash_firmware(image_path: str, probe_serial: str | None, address: int | str | None,
                    interface: str, released: dict[str, Any]) -> dict[str, Any]:
    # Checked before anything touches the probe, as run_firmware checks that
    # its ELF exists: a refusal after identify() would already have connected,
    # and possibly reset, for nothing.
    image = Path(image_path).expanduser()
    not_flashable = image_mod.check_flash_image(image)
    if not_flashable:
        return failed(not_flashable, image_path=str(image))
    raw = image.suffix.lower() == ".bin"
    runs = None if raw else image_mod.flash_runs(image)
    if runs is not None and address is not None:
        return failed(f"{image.name} carries the addresses it is written to, so `address` cannot choose "
                      "one. Pass a .bin to write at an address of your own.", image_path=str(image))
    image_size = image.stat().st_size if runs is None else sum(len(r.data) for r in runs)
    try:
        explicit = None if address is None else _address(address)
    except ValueError as exc:
        return failed(str(exc), image_path=str(image))

    try:
        toolchain = _toolchain()
    except ToolchainError as exc:
        return failed(str(exc))

    serial, _, error = _resolve_probe(toolchain, probe_serial)
    if error:
        return failed(error)
    # Only this probe matters. Another board's held server does not stop us.
    released.update(_stop_holder_first(serial))

    probe = Probe(toolchain, serial=serial, interface=interface, try_first=_settled_port(serial))
    try:
        info = probe.identify()
        _remember_port(serial, probe, info)
    except Exception as exc:  # noqa: BLE001
        return failed(str(exc), probe_serial=serial, **_after_failure(probe))
    if not info.connected:
        return failed(
            "No target",
            probe_serial=serial,
            target_was_reset=probe.target_was_reset,
            errors=info.errors,
            **_connection_fields(probe, info),
            hint=explain_connect_failure(info, interface),
        )

    quirks = info.quirks
    if runs is None:
        target_address = explicit if explicit is not None else quirks.flash_base
        if target_address is None:
            return failed(
                f"No flash base recorded for {quirks.name}; pass address explicitly."
            )
    else:
        target_address = None

    # The write path does not act on a value nobody has measured.
    #
    # Elsewhere this server will act on an unmeasured address and let the
    # observation decide, because a RAM load that used the wrong base is undone
    # by a reset and there IS an observation: the target either produces console
    # output or it does not. Neither is true here. This tool writes persistently,
    # and its readback confirms what was written, not that it went to the right
    # place.
    #
    # An explicit `address` is a different matter and is allowed: the caller has
    # said where to write, so it is their claim rather than the table's guess.
    # An ELF or HEX file names its own addresses, and the flash base is then
    # what they are checked against, so it must be measured.
    if explicit is None and not quirks.is_measured("flash_base"):
        return failed(
            f"The flash base for {quirks.name} is "
            f"{quirks.provenance_of('flash_base')}, not measured on hardware, and "
            "this tool writes permanently. Its readback confirms what was "
            "written, not that the address was the right one.",
            hint=(
                "Either pass a .bin with `address` explicitly, which records the "
                "choice as yours, or confirm the base by running an image from RAM "
                "first, which is reversible and self-verifying."
            ),
            **_after_failure(probe),
        )
    if runs is not None:
        refusal = _outside_flash(image, runs, quirks, info)
        if refusal:
            return failed(refusal, image_path=str(image), runs=_listed(runs), **_after_failure(probe))
    else:
        runs = [image_mod.Run(target_address, image.read_bytes())]

    # FF8: on the STM32N6, ST's external loader hangs the core once the board has
    # booted from its flash; the session is lost and only a power cycle brings the
    # board back (2026-10-06). So the boot pins are read before the loader runs.
    if quirks.external_loader is not None and _boot_mode(probe, quirks)[0] == "flash":
        return failed(
            f"The boot pins read flash boot. On {quirks.name}, ST's external loader hangs "
            "the core once the board has booted from its flash, and only a power cycle "
            "brings the board back, so nothing was written and the loader did not run.",
            next_step="Set development boot (BOOT1 high), power-cycle the board, and write again.",
            probe_serial=serial, boot_mode="flash", **_after_failure(probe),
        )

    # The erase takes whole sectors, so a small image takes the rest of its last
    # one with it: 16 KB written at 0x70000000 on an STM32N6 erased 64 KB, and
    # the result said nothing of it (2026-10-03). The bytes beside the image in
    # the blocks it touches are read before and after the write, and the
    # difference is reported.
    spans = _beside(runs)
    before = _read_spans(probe, quirks.external_loader, spans)

    try:
        output = probe.program_flash(str(image), target_address, quirks.external_loader)
    except Exception as exc:  # noqa: BLE001
        return failed(str(exc), probe_serial=serial, flash_state=_FLASH_MAY_HAVE_CHANGED, **_after_failure(probe))

    written: dict[str, Any] = {
        "probe_serial": serial,
        **_connection_fields(probe, info),
        "image_path": str(image),
        "image_size_bytes": image_size,
        **({"address": _hex(target_address)} if target_address is not None else {"runs": _listed(runs)}),
        "target_was_reset": probe.target_was_reset,
        "signing_required": quirks.requires_signing,
        "programmer_output": output.strip().splitlines()[-3:],
        "next_step": (
            "This does not start the image. "
            + (quirks.flash_boot_note if quirks.flash_boot_note else
               "This part boots only a signed image, which this tool does not make: sign it "
               "(ST's STM32_SigningTool_CLI), write the signed file, set the board's boot "
               "configuration to boot from flash and reset it."
               if quirks.requires_signing else
               "Set the board's boot configuration to boot from flash and reset it.")
        ),
    }

    # The programmer's "File download complete" is the vendor's own banner,
    # which the README's opening example shows cannot be trusted on its own, and
    # this is the one tool whose write persists. So flash is read back and
    # compared with the image, run by run, and only a byte-for-byte match of
    # every run is verified.
    for n, run in enumerate(runs):
        try:
            readback = probe.read_back(run.address, len(run.data), quirks.external_loader)
        except Exception as exc:  # noqa: BLE001
            checked = f" The {n} run(s) before it read back identical." if n else ""
            return unverified(
                "The programmer reported the write complete, but reading back "
                f"{len(run.data)} bytes from {_hex(run.address)} failed, so nothing checked "
                f"what flash holds there: {exc}.{checked}",
                **written,
            )
        if readback != run.data:
            differing = [i for i, (x, y) in enumerate(zip(run.data, readback)) if x != y]
            first = differing[0]
            return failed(
                f"Flash does not hold the image. Read back after the write, "
                f"{len(differing)} of {len(run.data)} bytes from {_hex(run.address)} differ, the first at "
                f"0x{run.address + first:08X}: the image has 0x{run.data[first]:02X}, "
                f"flash has 0x{readback[first]:02X}. The programmer reported the write "
                "complete. A protected or failing sector, or a write that did not "
                "erase first, would look like this.",
                first_difference=_hex(run.address + first),
                differing_bytes=len(differing),
                **written,
            )
    written.update(_erase_report(probe, quirks.external_loader, spans, before, output))
    if len(runs) == 1:
        return verified(
            f"read back {len(runs[0].data)} bytes from {_hex(runs[0].address)} after the write "
            "and compared them with the image: identical",
            **written,
        )
    return verified(
        f"read back each of the file's {len(runs)} runs, {image_size} bytes, after the write "
        "and compared them with the file: identical",
        **written,
    )


def _listed(runs: list[image_mod.Run]) -> list[dict[str, Any]]:
    return [{"address": _hex(r.address), "bytes": len(r.data)} for r in runs]


def _outside_flash(image: Path, runs: list[image_mod.Run], quirks: FamilyQuirks,
                   info: TargetInfo) -> str | None:
    """Why an ELF or HEX file's runs must not be written here, or None."""
    if info.nvm_size is None:
        return (f"The connect gave no flash size for {quirks.name}, so the addresses in {image.name} "
                "cannot be checked against its flash. Convert it to a .bin with "
                "`arm-none-eabi-objcopy -O binary` and pass `address`.")
    low, high = quirks.flash_base, quirks.flash_base + info.nvm_size
    outside = [r for r in runs if not (low <= r.address and r.end <= high)]
    if not outside:
        return None
    listed = ", ".join(f"{len(r.data)} bytes at 0x{r.address:08X}" for r in outside[:4])
    return (f"{image.name} writes outside this part's flash, 0x{low:08X} to 0x{high - 1:08X}: {listed}. "
            "An image linked for RAM, run_firmware's kind, looks like this. Nothing was written.")


#: The block flash_firmware looks across for what its erase took: 128 KB, the
#: largest sector on the parts measured (the H7's).
_ERASE_WINDOW = 0x20000

#: Spans closer than this are read in one call, the bytes between ignored.
_READ_TOGETHER = 0x1000


def _beside(runs: list[image_mod.Run]) -> list[tuple[int, int]]:
    """The bytes no run writes in the 128 KB blocks the runs touch, as
    [start, end) spans: what a sector erase may take without the image saying."""
    spans: list[tuple[int, int]] = []
    ordered = sorted(runs, key=lambda r: r.address)
    blocks = sorted({b for r in ordered for b in range(r.address // _ERASE_WINDOW, -(-r.end // _ERASE_WINDOW))})
    for block in blocks:
        cursor, high = block * _ERASE_WINDOW, (block + 1) * _ERASE_WINDOW
        for run in ordered:
            if run.end <= cursor or run.address >= high:
                continue
            if run.address > cursor:
                spans.append((cursor, run.address))
            cursor = max(cursor, run.end)
        if cursor < high:
            spans.append((cursor, high))
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and merged[-1][1] == start:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def _reads(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    reads: list[tuple[int, int]] = []
    for start, end in spans:
        if reads and start - reads[-1][1] < _READ_TOGETHER:
            reads[-1] = (reads[-1][0], end)
        else:
            reads.append((start, end))
    return reads


def _read_spans(probe: Probe, loader: str | None,
                spans: list[tuple[int, int]]) -> dict[int, int] | None:
    """Every byte of `spans`, by address, or None when a read failed."""
    found: dict[int, int] = {}
    try:
        for start, end in _reads(spans):
            data = probe.read_back(start, end - start, loader)
            found.update((start + i, b) for i, b in enumerate(data))
    except Exception:  # noqa: BLE001
        return None
    return {a: found[a] for start, end in spans for a in range(start, end) if a in found}


def _erase_report(probe: Probe, loader: str | None, spans: list[tuple[int, int]],
                  before: dict[int, int] | None, output: str) -> dict[str, Any]:
    """What the write's erase took beside the image, from reads before and after."""
    report: dict[str, Any] = {}
    erased = [line.strip() for line in output.splitlines() if line.strip().lower().startswith("erasing")]
    if erased:
        report["programmer_erase"] = erased
    if not spans:
        return report
    total = sum(end - start for start, end in spans)
    if before is None:
        report["erased_beyond_image"] = (
            f"not checked: the {total} bytes beside the image could not be read before the write"
        )
        return report
    after = _read_spans(probe, loader, spans)
    if after is None:
        report["erased_beyond_image"] = "not checked: reading them after the write failed"
        return report
    checked = {"checked_to": _hex(spans[-1][1]),
               **({"checked": [f"{_hex(a)} to {_hex(b)}" for a, b in spans]} if len(spans) > 1 else {})}
    changed = [a for a in sorted(before) if before[a] != after.get(a)]
    if not changed:
        blank = all(v == 0xFF for v in before.values())
        report["erased_beyond_image"] = {
            "bytes": 0, **checked,
            "note": ("These bytes were already blank, so whether the erase reached them does not show."
                     if blank else
                     "These bytes read the same before and after the write: the erase took nothing beyond the image."),
        }
        return report
    odd = sum(1 for a in changed if after.get(a) != 0xFF)
    report["erased_beyond_image"] = {
        "from": _hex(changed[0]), "to": _hex(changed[-1] + 1), "bytes": len(changed), **checked,
        "note": ("The erase takes whole sectors: these bytes beside the image held data before the "
                 "write and read 0xFF now." + (f" {odd} of them changed to something other than 0xFF, "
                                               "which an erase does not do." if odd else "")),
    }
    return report


@mcp.tool()
@_serialised
def read_memory(
    address: int | str | None = None,
    length_bytes: int = 32,
    symbol: str | None = None,
    elf_path: str | None = None,
    register: str | None = None,
    svd: str | None = None,
    read_effects_ok: bool = False,
    probe_serial: str | None = None,
    interface: Interface = "swd",
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Read target memory without halting or resetting it, given one of: `address`
(an integer or a "0x..." string, 4-byte aligned, `length_bytes` a multiple of
4, at most 1024); `symbol`, a variable decoded with the types in `elf_path`; or
`register`, such as "RCC.CR", decoded from ST's SVD file for the part (`svd`
picks one). Refuses a register whose read changes state,
such as a UART's data register, unless `read_effects_ok`. Refuses rather than
reset at a closed debug port.
    """
    given = [name for name, value in (("address", address), ("symbol", symbol), ("register", register))
             if value is not None]
    if len(given) != 1:
        return failed(
            "Give exactly one of address, symbol or register"
            + (f"; this call gave {' and '.join(given)}." if given else ".")
        )
    if symbol is not None:
        return _read_symbol(symbol, elf_path, probe_serial, interface, read_effects_ok)
    if register is not None:
        return _read_register(register, svd, probe_serial, interface, read_effects_ok)
    try:
        start = _address(address)
    except ValueError as exc:
        return failed(str(exc))
    if start % 4:
        return failed(f"address 0x{start:08X} is not 4-byte aligned; this reads 32-bit words.")
    if length_bytes <= 0 or length_bytes % 4:
        return failed(f"length_bytes is {length_bytes}; give a positive multiple of 4.")
    if length_bytes > _MAX_READ_BYTES:
        return failed(
            f"length_bytes is {length_bytes}; at most {_MAX_READ_BYTES} per call, "
            "so one read cannot flood the result. Read in pieces."
        )
    if start + length_bytes > 1 << 32:
        return failed(f"A {length_bytes}-byte read at 0x{start:08X} runs past the end of the address space.")

    # In the peripheral and device regions a read may change what it reads, so
    # the part is identified first and the range looked up in its SVD file.
    in_device = read_effects_mod.in_device_region(start, length_bytes)
    covered: dict[str, Any] = {}
    effects: list[dict[str, Any]] = []
    probe: Probe | None = None
    try:
        toolchain = _toolchain()
        serial, _, error = _resolve_probe(toolchain, probe_serial)
        if error:
            return failed(error)
        refusal = _held_refusal(serial)
        if refusal:
            return refusal
        probe = Probe(toolchain, serial=serial, interface=interface, try_first=_settled_port(serial),
                      shared=_shares(serial))
        one_call = None if in_device else probe.identify_and_read(start, length_bytes)
        if one_call is not None:
            info, words = one_call
        else:
            info = probe.identify(allow_reset=False)
            _remember_port(serial, probe, info)
            if not info.connected:
                return failed(
                    "Could not reach the core without resetting it, and this tool "
                    "does not reset. core_registers() will reset if it must, at the "
                    "cost of the state you are inspecting.",
                    probe_serial=serial,
                    **_connection_fields(probe, info),
                    hint=explain_connect_failure(info, interface),
                )
            if in_device:
                covered, effects = _registers_covered(toolchain, info, start, length_bytes, svd)
                if effects and not read_effects_ok:
                    return failed(_effects_refusal(effects), probe_serial=serial, read_effects=effects,
                                  **covered, **_connection_fields(probe, info))
            words = probe.read_memory(start, length_bytes)
    except Exception as exc:  # noqa: BLE001
        return failed(str(exc), **_after_failure(probe))

    # Only the words asked for, and all of them. A read that came back short
    # used to be verified with whatever it held, and the absent words were
    # simply not in the dict, which an agent summarising the result can miss.
    wanted = range(start, start + length_bytes, 4)
    missing = [a for a in wanted if a not in words]
    detail: dict[str, Any] = {
        "probe_serial": serial,
        "connect_mode": probe.connect_mode,
        **_connection_fields(probe, info),
        "target_was_reset": probe.target_was_reset,
        "words": {f"0x{a:08X}": f"0x{words[a]:08X}" for a in wanted if a in words},
        **covered,
        **_effects_disclosure(in_device, effects),
    }
    if missing:
        return failed(
            f"The read returned {len(wanted) - len(missing)} of the {len(wanted)} "
            "words asked for. The ones in `words` were read; the ones in "
            "`missing` were not, and are not zero.",
            missing=[f"0x{a:08X}" for a in missing],
            **detail,
        )
    if probe.degraded_reason:
        return unverified(probe.degraded_reason, **detail)

    return verified("words read back from the target", **detail)


def _registers_covered(toolchain: Toolchain, info: TargetInfo, start: int, length: int,
                       svd: str | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """For a read in the device region: result fields naming the registers it
    covers, and what reading each is known to do."""
    directory = toolchain.svd_dir
    try:
        files, how = svd_mod.choose(directory, svd=svd, device_name=info.device_name,
                                    board=info.board, cpu=info.cpu)
        vendor = svd_mod.vendor_marks(directory)
    except (svd_mod.SvdError, OSError) as exc:
        return {"registers_note": (
            f"{exc} So which registers this read covers, and whether reading them changes "
            "anything, is not known."
        )}, []
    found: dict[str, tuple[svd_mod.Register, str]] = {}
    undescribed: dict[str, int] = {}
    unmapped: int | None = None
    for path in files:
        coverage = svd_mod.covering(path, start, length)
        for register in coverage.registers:
            found.setdefault(f"{register.peripheral}.{register.name}", (register, path.name))
        for peripheral, count in coverage.undescribed.items():
            undescribed[peripheral] = max(undescribed.get(peripheral, 0), count)
        unmapped = coverage.unmapped if unmapped is None else min(unmapped, coverage.unmapped)
    effects = [{"register": key, **read_effects_mod.describe(effect)}
               for key, (register, file_name) in found.items()
               for effect in svd_mod.effects(register, file_name, vendor)]
    detail: dict[str, Any] = {"registers_read": list(found), "svd": ", ".join(f.name for f in files),
                              "svd_source": how}
    gaps = []
    if undescribed:
        gaps.append(f"{sum(undescribed.values())} of the words are in {', '.join(undescribed)} but in no "
                    "register this tool reads from the SVD file")
    if unmapped:
        gaps.append(f"{unmapped} are in no peripheral the file describes")
    if gaps:
        detail["registers_note"] = "; ".join(gaps) + ", so what reading them does is not known."
    return detail, effects


def _effects_refusal(effects: list[dict[str, Any]]) -> str:
    first = effects[0]
    more = f" ({len(effects) - 1} more in read_effects)" if len(effects) > 1 else ""
    return (
        f"Not read: this covers {first['register']}, and reading it {first['effect']} "
        f"({first['provenance']}: {first['established']}){more}. Pass read_effects_ok=true to "
        "read it anyway, or leave it out of the range."
    )


def _effects_disclosure(in_device: bool, effects: list[dict[str, Any]]) -> dict[str, Any]:
    if effects:
        return {"read_effects": effects,
                "read_effects_note": "Read because read_effects_ok was set: each effect listed has happened."}
    return {"read_side_effects_note": _READ_SIDE_EFFECT_NOTE} if in_device else {}


def _probe_for_read(
    probe_serial: str | None, interface: str
) -> tuple[tuple[Toolchain, str, Probe, TargetInfo] | None, dict[str, Any] | None]:
    """Resolve, refuse a probe held without sharing, and identify without reset,
    for a read by name."""
    toolchain = _toolchain()
    serial, _, error = _resolve_probe(toolchain, probe_serial)
    if error:
        return None, failed(error)
    refusal = _held_refusal(serial)
    if refusal:
        return None, refusal
    probe = Probe(toolchain, serial=serial, interface=interface, try_first=_settled_port(serial),
                  shared=_shares(serial))
    info = probe.identify(allow_reset=False)
    _remember_port(serial, probe, info)
    if not info.connected:
        return None, failed(
            "Could not reach the core without resetting it, and this tool does not reset.",
            probe_serial=serial,
            **_connection_fields(probe, info),
            hint=explain_connect_failure(info, interface),
        )
    return (toolchain, serial, probe, info), None


#: How much of an image's code is compared with the target to check that the
#: target runs the ELF a variable's address and type came from.
_IMAGE_CHECK_BYTES = 64


def _read_symbol(symbol: str, elf_path: str | None, probe_serial: str | None, interface: str,
                 read_effects_ok: bool = False) -> dict[str, Any]:
    if not elf_path:
        return failed("symbol needs elf_path: the ELF the target runs gives the variable's address and type.")
    try:
        variable = symbols_mod.find_variable(elf_path, symbol)
        code = symbols_mod.first_code(elf_path, _IMAGE_CHECK_BYTES)
    except symbols_mod.SymbolError as exc:
        return failed(str(exc))
    if variable.size > _MAX_READ_BYTES:
        return failed(
            f"{symbol} is {variable.size} bytes; at most {_MAX_READ_BYTES} are read by name, "
            "so one read cannot flood the result. Read it by address, in pieces."
        )
    start = variable.address & ~3
    end = (variable.address + variable.size + 3) & ~3
    ranges = [(start, end - start)]
    if code is not None:
        ranges.append((code[0] & ~3, (len(code[1]) + 3) & ~3))

    probe: Probe | None = None
    try:
        ready, refusal = _probe_for_read(probe_serial, interface)
        if refusal:
            return refusal
        toolchain, serial, probe, info = ready
        covered: dict[str, Any] = {}
        effects: list[dict[str, Any]] = []
        in_device = read_effects_mod.in_device_region(start, end - start)
        if in_device:
            covered, effects = _registers_covered(toolchain, info, start, end - start, None)
            if effects and not read_effects_ok:
                return failed(_effects_refusal(effects), probe_serial=serial, read_effects=effects, **covered)
        words = probe.read_ranges(ranges)
    except ProbeError as exc:
        # Measured 2026-10-02: an STM32H7 ELF's g_state, at 0x24000130, read on an
        # STM32F407, where nothing answers there.
        return failed(
            f"{symbol}'s address, 0x{variable.address:08X}, comes from {elf_path}, and "
            "nothing could be read there; an ELF built for another part does this. "
            + str(exc), **_after_failure(probe)
        )
    except Exception as exc:  # noqa: BLE001
        return failed(str(exc), **_after_failure(probe))

    def span(address: int, length: int) -> bytes | None:
        out = bytearray()
        for word_address in range(address & ~3, address + length, 4):
            if word_address not in words:
                return None
            out += words[word_address].to_bytes(4, "little")
        skip = address - (address & ~3)
        return bytes(out[skip:skip + length])

    data = span(variable.address, variable.size)
    detail: dict[str, Any] = {
        "probe_serial": serial,
        **_connection_fields(probe, info),
        "target_was_reset": probe.target_was_reset,
        "symbol": symbol,
        "address": f"0x{variable.address:08X}",
        "size_bytes": variable.size,
        "type": variable.type_name,
        "declared_in": variable.scope,
        "elf_path": elf_path,
        **covered,
        **_effects_disclosure(in_device, effects),
    }
    if data is None:
        return failed(f"The read did not return all {variable.size} bytes of {symbol}.", **detail)
    decoded = symbols_mod.decode(variable.type_die, data)
    detail["bytes"] = data.hex()
    on_target = span(code[0], len(code[1])) if code is not None else None
    if code is not None and on_target != code[1]:
        # Not `value`: an agent quoting the field without its verdict would
        # report another variable's bytes as this one.
        detail["decoded_with_this_elf"] = decoded
        return unverified(
            f"The bytes were read, but the target is not running this ELF: its first "
            f"{len(code[1])} bytes of code at 0x{code[0]:08X} differ from the ELF's, so the "
            "address and the type may not be this variable's. Load this ELF, or give the "
            "one the target runs.", **detail
        )
    detail["value"] = decoded
    if code is None:
        return unverified(
            "The bytes were read and decoded, but the ELF has no code segment to compare "
            "with the target, so nothing shows the target runs this ELF.", **detail
        )
    if probe.degraded_reason:
        return unverified(probe.degraded_reason, **detail)
    return verified(
        f"the variable's bytes read from the target, which runs this ELF: its first "
        f"{len(code[1])} bytes of code at 0x{code[0]:08X} match", **detail
    )


#: Every register read says this unless the file marks the read, which is refused.
_READ_SIDE_EFFECT_NOTE = (
    "No effect of reading this is known, which does not mean it has none: ST's SVD "
    "files mark few such registers, and this tool's own list is not complete."
)


def _read_register(register: str, svd: str | None, probe_serial: str | None, interface: str,
                   read_effects_ok: bool = False) -> dict[str, Any]:
    probe: Probe | None = None
    try:
        ready, refusal = _probe_for_read(probe_serial, interface)
        if refusal:
            return refusal
        toolchain, serial, probe, info = ready
        connection = {"probe_serial": serial, **_connection_fields(probe, info)}
        try:
            found, file_name, how = svd_mod.resolve(
                toolchain.svd_dir, register, svd=svd,
                device_name=info.device_name, board=info.board, cpu=info.cpu,
            )
            vendor = svd_mod.vendor_marks(toolchain.svd_dir)
        except svd_mod.SvdError as exc:
            return failed(str(exc), **connection)
        effects = [{"register": f"{found.peripheral}.{found.name}", **read_effects_mod.describe(e)}
                   for e in svd_mod.effects(found, file_name, vendor)]
        if effects and not read_effects_ok:
            return failed(_effects_refusal(effects), read_effects=effects, **connection)
        aligned = found.address & ~3
        words = probe.read_memory(aligned, 4)
    except Exception as exc:  # noqa: BLE001
        return failed(str(exc), **_after_failure(probe))

    detail: dict[str, Any] = {
        **connection,
        "target_was_reset": probe.target_was_reset,
        "register": f"{found.peripheral}.{found.name}",
        "address": f"0x{found.address:08X}",
        "description": found.description,
        "svd": file_name,
        "svd_source": how,
    }
    if aligned not in words:
        return failed(f"The read at 0x{aligned:08X} returned nothing for {register}.", **detail)
    value = (words[aligned] >> ((found.address & 3) * 8)) & ((1 << found.size_bits) - 1)
    detail.update({
        "value": f"0x{value:0{max(1, found.size_bits // 4)}X}",
        "fields": svd_mod.decode(found, value),
        **({"field_meanings": meanings} if (meanings := svd_mod.meanings(found, value)) else {}),
        **_effects_disclosure(True, effects),
    })
    if probe.degraded_reason:
        return unverified(probe.degraded_reason, **detail)
    return verified(f"the register read from the target and decoded with {file_name}", **detail)


#: sample_values' bounds: items per call, bytes read per sample, samples per call.
_SAMPLE_ITEMS, _SAMPLE_BYTES, _SAMPLE_MAX = 8, 2048, 1000
_DHCSR_ADDRESS = 0xE000EDF0


@dataclasses.dataclass
class _Sampled:
    """One thing sample_values reads: where, how much, and how to decode it."""
    name: str
    address: int
    size: int
    decode: Callable[[bytes], Any]
    hex: bool = False
    variable: str | None = None


@mcp.tool()
@_serialised
def sample_values(
    what: list[str],
    elf_path: str | None = None,
    seconds: float = 5.0,
    until: str | None = None,
    svd: str | None = None,
    read_effects_ok: bool = False,
    probe_serial: str | None = None,
    interface: Interface = "swd",
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Sample values while the program runs, without halting or resetting it: `what`
lists variables (decoded with `elf_path`'s types; "g_state.mode" picks a
member), "0x..." word addresses, or registers such as "ADC3.ISR" (`svd` and
`read_effects_ok` as in read_memory). All are read together, a few times a
second, for `seconds` (at most 60), with the core's state; each comes back with
first, last, range and changes. `until`, such as "g_state.mode == MODE_RUN" or
"g_ticks changes", stops early.
    """
    if not what:
        return failed("what is empty: name at least one variable, address or register.")
    if len(what) > _SAMPLE_ITEMS:
        return failed(f"what names {len(what)} values; at most {_SAMPLE_ITEMS} per call.")
    if not 0 < seconds <= 60:
        return failed(f"seconds is {seconds}; give more than 0 and at most 60.")
    try:
        condition = sampling_mod.parse_condition(until) if until else None
    except ValueError as exc:
        return failed(str(exc))

    items: list[_Sampled] = []
    registers: list[str] = []
    for entry in what:
        resolved = _sampled_without_probe(entry.strip(), elf_path)
        if isinstance(resolved, str):
            return failed(resolved)
        if resolved is None:
            registers.append(entry.strip())
        else:
            items.append(resolved)
    code = None
    if elf_path and any(i.variable for i in items):
        try:
            code = symbols_mod.first_code(elf_path, _IMAGE_CHECK_BYTES)
        except symbols_mod.SymbolError as exc:
            return failed(str(exc))

    probe: Probe | None = None
    try:
        ready, refusal = _probe_for_read(probe_serial, interface)
        if refusal:
            return refusal
        toolchain, serial, probe, info = ready
        connection = {"probe_serial": serial, **_connection_fields(probe, info)}
        effects: list[dict[str, Any]] = []
        for register in registers:
            try:
                found, file_name, _ = svd_mod.resolve(
                    toolchain.svd_dir, register, svd=svd,
                    device_name=info.device_name, board=info.board, cpu=info.cpu)
                vendor = svd_mod.vendor_marks(toolchain.svd_dir)
            except svd_mod.SvdError as exc:
                neither = f"{register} is no variable in {Path(elf_path).name}, and as a register: " if elf_path else f"{register}: "
                return failed(neither + str(exc), **connection)
            effects += [{"register": f"{found.peripheral}.{found.name}", **read_effects_mod.describe(e)}
                        for e in svd_mod.effects(found, file_name, vendor)]
            mask = (1 << found.size_bits) - 1
            items.append(_Sampled(register, found.address, max(1, found.size_bits // 8),
                                  lambda data, mask=mask: int.from_bytes(data, "little") & mask, hex=True))
        for item in items:
            if read_effects_mod.in_device_region(item.address, item.size) and item.name not in registers:
                effects += _registers_covered(toolchain, info, item.address & ~3, 4, svd)[1]
        if effects and not read_effects_ok:
            return failed(_effects_refusal(effects), read_effects=effects, **connection)
        ranges = sorted({(i.address & ~3, ((i.address + i.size + 3) & ~3) - (i.address & ~3)) for i in items})
        if sum(length for _, length in ranges) > _SAMPLE_BYTES:
            return failed(f"These values take {sum(n for _, n in ranges)} bytes a sample; at most "
                          f"{_SAMPLE_BYTES}. Watch fewer, or members rather than whole variables.", **connection)
        return _sample(probe, info, connection, items, ranges, code, condition, seconds, elf_path, effects)
    except Exception as exc:  # noqa: BLE001
        return failed(str(exc), **_after_failure(probe))


def _sampled_without_probe(entry: str, elf_path: str | None) -> _Sampled | str | None:
    """An address or a variable, resolved without the probe; None for what can
    only be a register, which needs the part's SVD file; a string to refuse with."""
    if re.fullmatch(r"0x[0-9A-Fa-f]+|\d+", entry):
        try:
            address = _address(entry)
        except ValueError as exc:
            return str(exc)
        if address % 4:
            return f"{entry} is not 4-byte aligned; an address is sampled as one 32-bit word."
        return _Sampled(f"0x{address:08X}", address, 4, lambda data: int.from_bytes(data, "little"), hex=True)
    base = re.match(r"[A-Za-z_]\w*", entry)
    if base is None:
        return f"{entry!r} is not a variable name, an address or a register such as ADC3.ISR."
    looks_like_register = re.fullmatch(r"[A-Za-z_]\w*\.[A-Za-z_]\w*", entry) is not None
    if not elf_path:
        return None if looks_like_register else f"{entry} needs elf_path: the ELF gives a variable's address and type."
    try:
        variable = symbols_mod.find_variable(elf_path, base.group(0))
    except symbols_mod.SymbolError as exc:
        return None if looks_like_register else str(exc)
    if variable.size > _SAMPLE_BYTES:
        return f"{base.group(0)} is {variable.size} bytes; sample its members, such as {base.group(0)}.<member>."
    return _Sampled(entry, variable.address, variable.size,
                    lambda data, die=variable.type_die: symbols_mod.decode(die, data), variable=base.group(0))


def _sample(probe: Probe, info: TargetInfo, connection: dict[str, Any], items: list[_Sampled],
            ranges: list[tuple[int, int]], code: tuple[int, bytes] | None,
            condition: sampling_mod.Condition | None, seconds: float, elf_path: str | None,
            effects: list[dict[str, Any]]) -> dict[str, Any]:
    """The sampling loop: one programmer call a sample, until `seconds` or `until`."""
    samples: list[tuple[float, dict[str, Any]]] = []
    states: list[str] = []
    stopped: str | None = None
    met: tuple[float, Any] | None = None
    start = time.monotonic()
    while True:
        extra = [(_DHCSR_ADDRESS, 4)] + ([(code[0] & ~3, (len(code[1]) + 3) & ~3)] if code and not samples else [])
        try:
            words = probe.read_ranges(ranges + extra)
        except Exception as exc:  # noqa: BLE001
            stopped = f"the read at {round(time.monotonic() - start, 2)} s failed: {exc}"
            break
        at = round(time.monotonic() - start, 2)
        if code and not samples and _span(words, code[0], len(code[1])) != code[1]:
            return unverified(
                f"The target is not running this ELF: its first {len(code[1])} bytes of code at "
                f"0x{code[0]:08X} differ from the ELF's, so the variables' addresses and types may "
                "not be theirs. Nothing was sampled. Load this ELF, or give the one the target runs.",
                elf_path=elf_path, target_was_reset=probe.target_was_reset, **connection)
        values: dict[str, Any] = {}
        for item in items:
            data = _span(words, item.address, item.size)
            if data is None:
                stopped = f"the read at {at} s did not return {item.name}"
                break
            decoded = item.decode(data)
            picked = {k: v for k, v in sampling_mod.leaves(item.variable or item.name, decoded).items()
                      if k == item.name or k.startswith((item.name + ".", item.name + "["))}
            if not picked:
                return failed(f"{item.name} names nothing in {item.variable}: its parts are "
                              f"{', '.join(list(sampling_mod.leaves(item.variable, decoded))[:12])}.",
                              **connection)
            values.update(picked)
        if stopped:
            break
        if condition and condition.leaf not in values:
            return failed(f"until names {condition.leaf}, which is not among the values sampled: "
                          f"{', '.join(list(values)[:12])}.", **connection)
        samples.append((at, values))
        dhcsr = words.get(_DHCSR_ADDRESS)
        states.append(CoreState(dhcsr, None, ()).state if dhcsr is not None else "not read")
        if condition:
            try:
                if condition.holds(values[condition.leaf], samples[0][1][condition.leaf]):
                    met = (at, values[condition.leaf])
                    break
            except ValueError as exc:
                return failed(str(exc), **connection)
        if time.monotonic() - start >= seconds or len(samples) >= _SAMPLE_MAX:
            break

    if not samples:
        return failed(f"Nothing was sampled: {stopped}.", **connection, **_after_failure(probe))
    hex_leaves = {i.name for i in items if i.hex}
    summary, hidden = sampling_mod.summarise(samples)
    for name in hex_leaves & summary.keys():
        summary[name] = {k: (f"0x{v:08X}" if k in ("value", "first", "last", "min", "max") and isinstance(v, int)
                             else [f"0x{x:08X}" for x in v] if k == "seen" else v)
                         for k, v in summary[name].items()}
    elapsed = samples[-1][0]
    interval = round(elapsed / (len(samples) - 1), 3) if len(samples) > 1 else None
    counted = {state: states.count(state) for state in dict.fromkeys(states)}
    detail: dict[str, Any] = {
        **connection,
        "target_was_reset": probe.target_was_reset,
        **({"elf_path": elf_path} if elf_path else {}),
        "samples": len(samples),
        "seconds": elapsed,
        **({"interval_s": interval} if interval is not None else {}),
        "values": summary,
        **({"values_not_shown": f"{hidden} more that never changed"} if hidden else {}),
        "core": counted,
        **({"core_note": "The core was not running in every sample, so a value that did not change "
                         "may not have had code running to change it."}
           if set(counted) - {"running"} else {}),
        **({"until": {"condition": str(condition), "held_at_s": met[0],
                      "value": f"0x{met[1]:08X}" if condition.leaf in hex_leaves and isinstance(met[1], int)
                      else met[1]}} if met else {}),
    }
    if effects:
        detail.update(read_effects=effects, read_effects_note="Read because read_effects_ok was set: each "
                      "effect listed happened at every sample.")
    unseen = (f" A value that changed and changed back between two samples, about {interval} s "
              "apart, would not show." if interval is not None else "")
    if stopped:
        return unverified(f"Sampling stopped after {len(samples)} samples: {stopped}. The values "
                          "cover the samples read until then.", **detail)
    if condition and not met:
        detail["until"] = {"condition": str(condition), "held": False}
        return unverified(f"until {condition} was not seen in {len(samples)} samples over {elapsed} s."
                          + unseen, **detail)
    if probe.degraded_reason:
        return unverified(probe.degraded_reason, **detail)
    return verified(
        f"{len(samples)} samples over {elapsed} s, each read in one programmer call without halting the core"
        + (", the code checked against the ELF's at the first" if code else "")
        + (f"; until {condition} held at {met[0]} s" if met else "") + "." + unseen, **detail)


def _span(words: dict[int, int], address: int, length: int) -> bytes | None:
    """`length` bytes at `address` from words read, or None if any is missing."""
    out = bytearray()
    for word_address in range(address & ~3, address + length, 4):
        if word_address not in words:
            return None
        out += words[word_address].to_bytes(4, "little")
    skip = address - (address & ~3)
    return bytes(out[skip:skip + length])


#: Where run_until left a core halted, per probe. release_probe and run_firmware
#: clear it; core_registers reads it to leave such a core halted.
_stopped: dict[str | None, dict[str, Any]] = {}

def _held_here(place: str) -> str:
    return (f"halted in {place}, held by run_until; timers, DMA and watchdogs keep "
            "running. run_until again continues from here; release_probe resumes it.")

_WRITE_NOTE = (
    "The write was one to four instructions before this stop: just before stopped_at, "
    "or in returned_from if that call returned in between. The source of both tells which."
)


def _debug_server(toolchain: Toolchain, serial: str | None, interface: str):
    """(server, failure or None, started): the server holding the probe, of
    either kind, since every server keeps the log that records the chip's reason
    for a stop; or, when none holds it, a new one in attach mode, which halts the
    core without a reset. Using the held one never lets go of the probe, which a
    program that closes the debug port would not give back."""
    held = held_server(serial)
    if held is not None:
        mismatch = interface_mismatch(held, interface)
        return held, failed(mismatch, probe_serial=serial) if mismatch else None, False
    probe = Probe(toolchain, serial=serial, interface=interface, try_first=_settled_port(serial))
    info = probe.identify(allow_reset=False)
    _remember_port(serial, probe, info)
    if not info.connected:
        return None, failed(
            "Could not reach the core without resetting it, and this tool does not reset.",
            probe_serial=serial, **_connection_fields(probe, info),
            hint=explain_connect_failure(info, interface),
        ), False
    server = acquire_server(
        toolchain, probe.gdb_access_port(info), _gdb_port_for(serial),
        serial=serial, interface=interface, attach=True,
    )
    with _STATE:
        _held[serial] = (probe, info, None)
        _held_by.setdefault(serial, current_caller())
    return server, None, True


#: core_after for a halt made through the programmer, when DHCSR cannot be read
#: afterwards. Without ST's shared mode such a halt ended with the command (two
#: H7 boards, 2026-08-17); in it, it did not (N6 and F407, 2026-10-04), so a core
#: that was running is let go after the read.
_PROGRAMMER_HALT_AFTER = (
    "not observed: the core was halted for the read and let go afterwards if it had "
    "been running. run_until holds a core stopped."
)

_NO_ATTACH = (
    "GDB could not reach the core through the GDB server. A program that takes the SWD "
    "pins, stops a clock or sleeps without debug does this, and read_memory and board_state "
    "fail the same way. release_probe, then board_state, gets back in with a connect under "
    "reset, which restarts the program."
)

_DETACH_RESUMED = (
    "resumed: GDB's detach resumed the core on the H7, the F407 and the N6; "
    "not observed for this call"
)


def _halted_now(toolchain: Toolchain, serial: str | None, interface: str) -> bool | None:
    """Whether the core is halted, from DHCSR read without halting it; None when
    that read fails."""
    port = _settled_port(serial)
    if port is None:
        return None
    try:
        probe = Probe(toolchain, access_port=port, serial=serial, interface=interface, shared=_shares(serial))
        dhcsr = probe.read_memory(_DHCSR, 4).get(_DHCSR)
    except Exception:  # noqa: BLE001
        return None
    return None if dhcsr is None else bool(dhcsr & _S_HALT)


def _core_after_observed(probe: Probe | None, toolchain: Toolchain, serial: str | None,
                         interface: str, fallback: str) -> str:
    """What the core does after a call that halted it, from DHCSR read without
    halting it; `fallback`, the measured expectation, when that read fails.
    Agents in an evaluation named "not observed" as leaving them unsure whether
    the core ran (2026-10-04)."""
    try:
        if probe is None:
            port = _settled_port(serial)
            if port is None:
                return fallback
            probe = Probe(toolchain, access_port=port, serial=serial, interface=interface,
                          shared=_shares(serial))
        dhcsr = probe.read_memory(_DHCSR, 4).get(_DHCSR)
    except Exception:  # noqa: BLE001
        return fallback
    if dhcsr is None:
        return fallback
    return f"{CoreState(dhcsr, None, ()).state}: DHCSR 0x{dhcsr:08X}, read without halting after the call"


def _drop_debug_server(serial: str | None) -> None:
    """Stop a server a refused call started, so the probe is left as it was
    found; stopping it lets the program run on (measured)."""
    release_server(serial)
    with _STATE:
        _held.pop(serial, None)
        _held_by.pop(serial, None)


def _elf_on_target(session: gdbmi.Session, elf_path: str) -> str | None:
    """None when the target holds the start of this ELF's code; otherwise why not."""
    code = symbols_mod.first_code(elf_path, _IMAGE_CHECK_BYTES)
    if code is None:
        return "the ELF has no code segment to compare with the target"
    if debugger_mod.read_bytes(session, code[0], len(code[1])) != code[1]:
        return (f"the target is not running this ELF: its first {len(code[1])} bytes of code "
                f"at 0x{code[0]:08X} differ from the ELF's")
    return None


def _registers_via_gdb(toolchain: Toolchain, serial: str | None, interface: str,
                       elf_path: str | None) -> dict[str, Any]:
    if elf_path is not None:
        try:
            symbols_mod.first_code(elf_path, 4)
        except symbols_mod.SymbolError as exc:
            return failed(str(exc))
    server = held_server(serial)
    mismatch = interface_mismatch(server, interface) if server is not None else None
    if mismatch:
        return failed(mismatch, probe_serial=serial)
    temporary = server is None
    session: gdbmi.Session | None = None
    with _STATE:
        was_halted = serial in _stopped
    # Left as it was found: GDB's detach resumes the core, and one the held
    # server had halted at its reset vector after a lockup was set running into
    # its flash program by a register read (2026-10-04).
    found_halted = not was_halted and _halted_now(toolchain, serial, interface) is True
    was_halted = was_halted or found_halted
    try:
        if temporary:
            server, refusal, _started = _debug_server(toolchain, serial, interface)
            if refusal:
                return refusal
        session = gdbmi.Session(toolchain.gdb, elf_path, server.port)
        mismatch = _elf_on_target(session, elf_path) if elf_path else None
        regs = debugger_mod.registers(session) | debugger_mod.system_registers(session)
        stack, cut = debugger_mod.frames(session) if elf_path and not mismatch else ([], False)
        session.close(detach=not was_halted)
        session = None
    except (gdbmi.MiError, RunError, ProbeError, OSError) as exc:
        if session is not None:
            session.close(detach=not was_halted)
        return failed(str(exc), probe_serial=serial)
    finally:
        if temporary and server is not None:
            _drop_debug_server(serial)

    detail: dict[str, Any] = {
        "probe_serial": serial,
        "interface": server.interface,
        "halted_during_call": True,
        "core_after": ("still halted, as it was found" if found_halted else
                       "still halted where run_until stopped it" if was_halted else
                       _core_after_observed(None, toolchain, serial, interface, _DETACH_RESUMED)),
        "target_was_reset": False,
        "registers": {name: f"0x{value:08X}" for name, value in regs.items()},
    }
    if elf_path is not None:
        detail["elf_path"] = elf_path
        detail["call_stack"] = stack
        if cut:
            detail["call_stack_cut"] = f"deeper than {debugger_mod.MAX_FRAMES} frames; the rest is not shown"
    if mismatch:
        return unverified(f"The registers were read, but {mismatch}, so no call stack is given.", **detail)
    return verified(
        "registers read through GDB from the halted core"
        + (", with the call stack from this ELF's debug information" if elf_path else ""),
        **detail,
    )


@mcp.tool()
@_serialised
def run_until(
    elf_path: str,
    location: str | None = None,
    watch: str | None = None,
    seconds: float = 10.0,
    probe_serial: str | None = None,
    interface: Interface = "swd",
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Run the target until it reaches `location` (a function, its inlined copies
included, "file.c:line" or a "0x..." address) or writes `watch` (a variable, or a
"0x..." address watched as 4 bytes), for at most `seconds`. The core is left halted where it stopped, with the call stack, until
release_probe. Needs the ELF the target runs. Verified only when the chip's
DFSR names the stop; a watchpoint stops just after the write.
The rest of the chip keeps running.
    """
    if (location is None) == (watch is None):
        return failed("Give one of location or watch.")
    too_long = _capture_seconds_problem(seconds, "seconds")
    if too_long:
        return failed(too_long)
    try:
        symbols_mod.first_code(elf_path, 4)
    except symbols_mod.SymbolError as exc:
        return failed(str(exc))

    serial: str | None = None
    session: gdbmi.Session | None = None
    try:
        toolchain = _toolchain()
        serial, _, error = _resolve_probe(toolchain, probe_serial)
        if error:
            return failed(error)
        server, refusal, started = _debug_server(toolchain, serial, interface)
        if refusal:
            return refusal
        with _STATE:
            was_halted = serial in _stopped
        session = gdbmi.Session(toolchain.gdb, elf_path, server.port)
        mismatch = _elf_on_target(session, elf_path)
        if mismatch:
            session.close(detach=not was_halted)
            session = None
            if started:
                _drop_debug_server(serial)
            return failed(
                f"Refused: {mismatch}, so a breakpoint would land in another program's code. "
                "Load this ELF, or give the one the target runs.", probe_serial=serial,
                **_after_attach(was_halted),
            )
        with _STATE:
            held_info = (_held.get(serial) or (None, None, None))[1]

        def read_effects(address: int, size: int) -> list[dict[str, Any]]:
            if held_info is None:
                return []
            return _registers_covered(toolchain, held_info, address, size, None)[1]

        try:
            # A function's inlined copies too: GDB breaks on its out-of-line copy only.
            also_at = (symbols_mod.inlined_at(elf_path, location)
                       if location is not None and re.fullmatch(r"[A-Za-z_]\w*", location) else ())
            stop = debugger_mod.run_until(session, server, location=location, watch=watch, seconds=seconds,
                                          read_effects=read_effects, also_at=also_at)
        except debugger_mod.DebugError as exc:
            session.close(detach=not was_halted)
            session = None
            if started:
                _drop_debug_server(serial)
            return failed(str(exc), probe_serial=serial, **_after_attach(was_halted))
        regs = debugger_mod.registers(session) | debugger_mod.system_registers(session)
        session.close(detach=False)
        session = None
    except (gdbmi.MiError, RunError, ProbeError, ToolchainError, OSError) as exc:
        after: dict[str, Any] = {}
        if session is not None:
            # GDB halted the core when it attached, and the error can come at any
            # point after that. Disconnecting left it halted where no result said;
            # detaching resumes it, and says so.
            session.close(detach=True)
            with _STATE:
                _stopped.pop(serial, None)
            if started:
                _drop_debug_server(serial)
            after.update(halted_during_call=True, core_after=_DETACH_RESUMED)
        elif "-target-select" in str(exc):
            # GDB never attached. On an H7 whose program had taken the SWD pins
            # this came back as a bare GDB timeout (2026-10-04).
            after["next_step"] = _NO_ATTACH
        return failed(str(exc), probe_serial=serial, **after,
                      **({"gdb_server_held": True} if serial and server_is_held(serial) else {}))

    top = stop.frames[0] if stop.frames else {}
    where = {"address": f"0x{stop.pc:08X}" if stop.pc is not None else None,
             "function": top.get("function"), "file": top.get("file"), "line": top.get("line")}
    instruction = _instruction(toolchain, Path(elf_path), stop.pc) if stop.pc is not None else None
    if instruction:
        where["instruction"] = instruction
    with _STATE:
        _stopped[serial] = {"address": where["address"], "function": where["function"]}
    exception = regs.get("XPSR", 0) & 0x1FF
    detail: dict[str, Any] = {
        "probe_serial": serial,
        "interface": server.interface,
        "elf_path": elf_path,
        **stop.detail,
        "stopped_at": where,
        "call_stack": stop.frames,
        "seconds_waited": round(stop.seconds, 2),
        "stop_reason_from_chip": [
            f"0x{v:08X} ({'+'.join(debugger_mod.dfsr_names(v))})" for v in stop.dfsr
        ],
        "registers": {name: f"0x{value:08X}" for name, value in regs.items()},
        "halted_during_call": True,
        "core_after": None,
        "target_was_reset": False,
    }
    if stop.frames_cut:
        detail["call_stack_cut"] = f"deeper than {debugger_mod.MAX_FRAMES} frames; the rest is not shown"
    if exception:
        detail["in_exception"] = f"{exception} ({faults_mod.exception_name(exception)})"
    what = location if location is not None else f"a write to {watch}"
    place = f"{where['function'] or where['address']}" + (f" ({where['file']}:{where['line']})" if where["file"] else "")
    detail["core_after"] = _held_here(place)
    if not stop.reached:
        reason = f"{what} was not reached in {seconds:g} s. The core was interrupted and is halted in {place}."
        if exception:
            reason += (f" It is in the {faults_mod.exception_name(exception)} handler: release_probe, "
                       "then read_fault_status and locate_fault, say why.")
        return unverified(reason, **detail)
    if not stop.dfsr:
        return unverified(
            f"The core stopped in {place}, but the GDB server's log has no stop reason for it, "
            "so the chip's own account of the stop is missing.", **detail
        )
    bits = 0
    for value in stop.dfsr:
        bits |= value
    logged = ", ".join(f"0x{v:08X}" for v in stop.dfsr)
    if location is not None:
        if bits & debugger_mod.DFSR_BKPT and stop.detail.get("at_location"):
            return verified(
                f"the core stopped at {location}, {where['address']}, and the chip's Debug Fault "
                f"Status Register, as the GDB server read it at the stop, names a breakpoint ({logged})",
                **detail,
            )
        return failed(
            f"The core stopped at {where['address']} in {place}, not at {location}; the chip's Debug "
            f"Fault Status Register read {logged}.", **detail
        )
    if bits & debugger_mod.DFSR_DWTTRAP:
        detail["write_note"] = _WRITE_NOTE
        return verified(
            f"the core stopped after a write to {watch}, and the chip's Debug Fault Status Register, "
            f"as the GDB server read it at the stop, names a watchpoint ({logged})",
            **detail,
        )
    return failed(
        f"The core stopped in {place}, but not for the watchpoint: the chip's Debug Fault Status "
        f"Register read {logged}.", **detail
    )


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
    effects = read_effects_mod.load_user_effects(path)
    quirks_mod.set_user_boards(boards)
    read_effects_mod.set_user_effects(effects)
    return len(boards), path


@mcp.tool()
@_serialised
def reset_target(
    probe_serial: str | None = None,
    interface: Interface = "swd",
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Reset the target and check that it restarted: the program counter is compared
with the reset vector during a brief halt, which is reported, and the core is
then left running from reset. Needs an open debug port: when the running
program has closed it, board_state reconnects through a reset, and this tool
does not. Stops a GDB server holding the probe first
(`released_probe_first`). To stop the core somewhere, use run_until.
    """
    released: dict[str, Any] = {}
    return {**_reset_target(probe_serial, interface, released), **released}


def _reset_target(probe_serial: str | None, interface: str, released: dict[str, Any]) -> dict[str, Any]:
    probe: Probe | None = None
    try:
        toolchain = _toolchain()
        serial, _, error = _resolve_probe(toolchain, probe_serial)
        if error:
            return failed(error)
        released.update(_stop_holder_first(serial))

        probe = Probe(toolchain, serial=serial, interface=interface, try_first=_settled_port(serial))
        # allow_reset=False deliberately: identification must not reset the
        # target before this tool has decided to, or the thing being verified
        # has already happened once, unobserved.
        info = probe.identify(allow_reset=False)
        _remember_port(serial, probe, info)
        if not info.connected:
            return failed(
                "Cannot reach the target to reset it.",
                probe_serial=serial,
                **_connection_fields(probe, info),
                hint=explain_connect_failure(info, interface),
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
        return failed(str(exc), **_after_failure(probe))

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
        **_connection_fields(probe, info),
        "family": quirks.name,
        "reset_kind": "hardware pulse on nRST (HWRSTPULSE connect)",
        "program_counter": _hex(pc),
        "expected_reset_handler": _hex(None if expected is None else expected & ~1),
        # What was DONE, not what the core is doing now. The difference is the
        # whole point: these two are observations, and "core_halted" was a guess
        # wearing a field name.
        "halted_during_call": halted_to_read_pc,
        "core_after": (
            "running: halted only to read the program counter, and the programmer "
            "reported it resumed" if released else
            "not observed: halted to read the program counter, and the release that "
            "followed failed" if halted_to_read_pc else
            "not observed: the core was not reached after the reset"
        ),
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
            hint = (
                f"On {quirks.name} the debug access port was most likely closed "
                "ALREADY: by ST's start-up code in an image that ran before this "
                "call, which only removing power undoes, or, booted from flash, by "
                "an image that does not open debug, which development boot gets "
                "past. This is not evidence that the reset failed, and it is not "
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
@_serialised
def core_registers(
    probe_serial: str | None = None,
    interface: Interface = "swd",
    elf_path: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Read the core registers, halting the core for the read; with `elf_path`, also
the call stack with each frame's arguments and locals. Says whether the core
runs again afterwards. Works while run_until holds the core halted, and leaves
it so. Without `elf_path`, a core it cannot reach as it runs is reached through
a reset, which `target_was_reset` reports; with `elf_path` it
refuses instead. Halts the core, so not for routine checks.
    """
    probe: Probe | None = None
    try:
        toolchain = _toolchain()
        serial, _, error = _resolve_probe(toolchain, probe_serial)
        if error:
            return failed(error)
        if elf_path is not None or held_server(serial) is not None:
            return _registers_via_gdb(toolchain, serial, interface, elf_path)
        probe = Probe(toolchain, serial=serial, interface=interface, try_first=_settled_port(serial))
        # The connect is checked before the read. It used to be discarded, so a
        # target nothing could reach surfaced as a bare parse failure from the
        # register read, without the connect errors, the hint, or the fact that
        # identify() had pulsed reset trying to reach it.
        info = probe.identify()
        _remember_port(serial, probe, info)
        if not info.connected:
            return failed(
                "Could not reach the core.",
                probe_serial=serial,
                target_was_reset=probe.target_was_reset,
                errors=info.errors,
                **_connection_fields(probe, info),
                hint=explain_connect_failure(info, interface),
            )
        regs = probe.core_registers()
    except Exception as exc:  # noqa: BLE001
        return failed(str(exc), **_after_failure(probe))

    detail: dict[str, Any] = {
        "probe_serial": serial,
        # What was DONE, not a claim about the core now. This used to be
        # `"halted": True` beside "The core is now halted", the same unobserved
        # claim reset_target dropped on 2026-08-17 after two H7 boards showed a
        # programmer halt does not outlive the command that made it
        # (docs/findings.md, "A halt does not survive the invocation").
        "halted_during_call": True,
        "core_after": _core_after_observed(probe, toolchain, serial, interface, _PROGRAMMER_HALT_AFTER),
        "connect_mode": probe.connect_mode,
        **_connection_fields(probe, info),
        # Reported, not hidden. Reaching the family's access port on a part that
        # closes it can need a resetting connect, and a reset restarts the very
        # program being inspected. A caller cannot allow for that if it is not told.
        "target_was_reset": probe.target_was_reset,
        "registers": {name: f"0x{value:08X}" for name, value in regs.items()},
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
@_serialised
def read_fault_status(
    probe_serial: str | None = None,
    interface: Interface = "swd",
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Read and decode the Cortex-M fault status registers without halting or
resetting the target. They stay set until software or a reset clears them, so
call this first after a crash; a probe run_firmware still holds resets a core
that locks up, which clears them. Says whether a fault is active, what happened
in plain words, the faulting address when the hardware recorded it, and how to
find the instruction. Refuses Cortex-M0, M0+, M1 and M23 cores.
    """
    probe: Probe | None = None
    try:
        toolchain = _toolchain()
        serial, _, error = _resolve_probe(toolchain, probe_serial)
        if error:
            return failed(error)
        refusal = _held_refusal(serial)
        if refusal:
            return refusal
        probe = Probe(toolchain, serial=serial, interface=interface, try_first=_settled_port(serial),
                      shared=_shares(serial))
        # In one programmer call where nothing could differ; the one-call path
        # runs only for a family whose access port was measured, and every such
        # family has fault registers, so the refusal below still comes first
        # wherever it can apply.
        one_call = probe.identify_and_read(
            faults_mod.FAULT_REGION_BASE, faults_mod.FAULT_REGION_SIZE
        )
        if one_call is not None:
            info, words = one_call
        else:
            info = probe.identify(allow_reset=False)
            _remember_port(serial, probe, info)
            if not info.connected:
                return failed(
                    "Could not reach the core without resetting it. Reading the "
                    "fault registers requires a debug connection, and resetting "
                    "clears them, which would destroy the information this tool "
                    "exists to read.",
                    probe_serial=serial,
                    **_connection_fields(probe, info),
                    hint=explain_connect_failure(info, interface),
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
        return failed(str(exc), **_after_failure(probe))

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
        **_connection_fields(probe, info),
        "target_was_reset": probe.target_was_reset,
        "any_fault_active": status.any_fault,
        "diagnosis": status.diagnosis,
        "faults": [
            {"category": f.category, "bit": f.name, "meaning": f.meaning}
            for f in status.faults
        ],
        # MMFAR and BFAR only when CFSR marks them valid: otherwise they hold a
        # stale or reset value, which three of four agents in an evaluation named
        # as easy to take for the faulting address (2026-10-04). A valid one is also given
        # as memmanage_fault_address or bus_fault_address.
        "raw_registers": {
            name: f"0x{val:08X}" for name, val in status.raw.items()
            if (name != "MMFAR" or status.mmfar is not None) and (name != "BFAR" or status.bfar is not None)
        },
        "note": (
            "Sticky until software clears them or the target resets: a clean read means "
            "no fault since then, not a healthy program."
        ),
    }

    if status.mmfar is not None:
        detail["memmanage_fault_address"] = f"0x{status.mmfar:08X}"
    if status.bfar is not None:
        detail["bus_fault_address"] = f"0x{status.bfar:08X}"

    if status.debug:
        # DFSR is sticky like the rest: HALTED here meant a halt at some time since
        # it was cleared, and read under the old name as the core being halted now.
        detail["debug_events_since_cleared"] = [
            {"bit": d.name, "meaning": d.meaning} for d in status.debug
        ]

    caught = probe.shared and any(d.name == "VCATCH" for d in status.debug)
    if caught:
        detail["vector_catch_note"] = _VECTOR_CATCH_NOTE

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
            "Call locate_fault() while the core is still in the fault handler: it finds "
            "the faulting instruction from the exception frame, on MSP or PSP as "
            "EXC_RETURN says, with its function and line given the ELF."
        )

    if probe.degraded_reason:
        return unverified(probe.degraded_reason, **detail)

    # A clean read beside a held server that caught a vector is not evidence of
    # no fault: that is how a lockup looked once the server had reset the core.
    # It came back verified, "no fault is active", and an agent read the headline
    # as healthy (2026-10-04).
    if caught and not status.any_fault:
        return unverified(
            "The fault registers read clean, but DFSR records a vector catch while a GDB server "
            "held the probe, which is what a lockup left once the server reset the core. Run "
            "again with run_firmware(keep_probe=False) to see such a fault.",
            **detail,
        )

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
@_serialised
def read_option_bytes(
    probe_serial: str | None = None,
    interface: Interface = "swd",
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Read the option bytes as ST's programmer reads and explains them: readout
protection first, then each section, such as brown-out level, user
configuration, boot addresses and write protection, with every value and its
meaning. Read only: no tool here writes option bytes, since some writes cannot
be undone. Never halts or resets the target. Fails, with ST's reason, on a part
the programmer has no option bytes for, such as the STM32N6.
    """
    probe: Probe | None = None
    try:
        toolchain = _toolchain()
        serial, _, error = _resolve_probe(toolchain, probe_serial)
        if error:
            return failed(error)
        refusal = _held_refusal(serial)
        if refusal:
            return refusal
        probe = Probe(toolchain, serial=serial, interface=interface, try_first=_settled_port(serial),
                      shared=_shares(serial))
        info = probe.identify(allow_reset=False)
        _remember_port(serial, probe, info)
        if not info.connected:
            return failed(
                "Could not reach the core without resetting it, and this tool does not reset.",
                probe_serial=serial, **_connection_fields(probe, info),
                hint=explain_connect_failure(info, interface),
            )
        output = probe.option_bytes()
    except Exception as exc:  # noqa: BLE001
        return failed(str(exc), **_after_failure(probe))

    sections = parse_option_bytes(output)
    detail: dict[str, Any] = {
        "probe_serial": serial,
        "device": info.device_name,
        **_connection_fields(probe, info),
        "target_was_reset": probe.target_was_reset,
    }
    if not sections:
        lines = [line.strip() for line in strip_ansi(output).splitlines() if line.strip()]
        errors = [line for line in lines if line.startswith("Error")]
        return failed(errors[0] if errors else "The programmer printed no option bytes.",
                      programmer_output=errors or lines[-5:], **detail)
    rdp = next((fields["RDP"] for fields in sections.values() if "RDP" in fields), None)
    detail.update(read_protection=rdp, option_bytes=sections)
    count = sum(len(fields) for fields in sections.values())
    if rdp is None:
        return unverified(
            f"The programmer printed {count} option-byte values and no readout protection (RDP) "
            "among them, which every STM32 it was run on had; the rest are given as read.",
            **detail,
        )
    return verified(f"the option bytes as the programmer read them from the device: {count} values", **detail)


@mcp.tool()
@_serialised
def locate_fault(
    probe_serial: str | None = None,
    elf_path: str | None = None,
    interface: Interface = "swd",
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Find the instruction that faulted, from the exception frame. Halts the core to
read registers, and never resets it, since a reset loses the fault: refuses
when the core cannot be reached without one. Call it while the core sits in a
fault handler. Returns the stacked PC and LR, from the stack that EXC_RETURN in
LR names; with `elf_path`, their functions and source lines. Refuses when the
core is not in a handler or LR no longer holds EXC_RETURN.
    """
    elf: Path | None = None
    if elf_path:
        elf = Path(elf_path).expanduser()
        if not elf.is_file():
            return failed(f"No such ELF: {elf}")
    probe: Probe | None = None
    try:
        toolchain = _toolchain()
        serial, _, error = _resolve_probe(toolchain, probe_serial)
        if error:
            return failed(error)
        refusal = _held_refusal(serial)
        if refusal:
            return refusal
        probe = Probe(toolchain, serial=serial, interface=interface, try_first=_settled_port(serial),
                      shared=_shares(serial))
        # Never a resetting connect: a reset restarts the program, and the fault
        # and its frame go with it. It used to reset to get in and then refuse.
        info = probe.identify(allow_reset=False)
        _remember_port(serial, probe, info)
        if not info.connected:
            return failed(
                "Could not reach the core without resetting it, and this tool does not reset: a "
                "reset restarts the program, and the fault and its frame go with it.",
                probe_serial=serial,
                target_was_reset=probe.target_was_reset,
                errors=info.errors,
                **_connection_fields(probe, info),
                hint=explain_connect_failure(info, interface),
            )
        regs = probe.core_registers()
    except Exception as exc:  # noqa: BLE001
        return failed(str(exc), **_after_failure(probe))

    base: dict[str, Any] = {
        "probe_serial": serial,
        "cpu": info.cpu,
        "connect_mode": probe.connect_mode,
        **_connection_fields(probe, info),
        "target_was_reset": probe.target_was_reset,
        "halted_during_call": True,
        "core_after": _core_after_observed(probe, toolchain, serial, interface, _PROGRAMMER_HALT_AFTER),
        "registers": {
            name: f"0x{regs[name]:08X}"
            for name in ("PC", "LR", "XPSR", "MSP", "PSP") if name in regs
        },
    }
    # A reset restarts the program, and with it goes the handler, the frame and
    # the fault. Whatever is read after one describes the restarted program.
    if probe.target_was_reset:
        return failed(
            "Reaching the core needed a resetting connect, which restarted the "
            "program, so the fault and its frame are gone.",
            **base,
        )
    missing = [name for name in ("XPSR", "LR", "MSP", "PSP") if name not in regs]
    if missing:
        return failed(
            f"The register read did not return {', '.join(missing)}.", **base
        )

    number = regs["XPSR"] & 0x1FF
    base["exception"] = {"number": number, "name": faults_mod.exception_name(number)}
    if number == 0:
        return failed(
            "The core is in thread mode, not in an exception handler, so there is no "
            "exception frame to read. A fault read_fault_status reports, if any, "
            "happened earlier, and its handler has returned or the core was reset since.",
            **base,
        )
    lr = regs["LR"]
    exc_return = faults_mod.decode_exc_return(lr, faults_mod.is_armv8m(info.cpu))
    if exc_return is None:
        return failed(
            f"LR is 0x{lr:08X}, not an EXC_RETURN value (0xFFxxxxxx): the handler "
            "has overwritten it, so the frame cannot be located from it. A handler "
            "that saves LR first thing keeps this possible.",
            **base,
        )

    sp = regs[exc_return.stack]
    frame_address = sp + exc_return.extra_state_bytes
    base.update(
        exc_return=f"0x{lr:08X}",
        stack=exc_return.stack,
        frame_address=_hex(frame_address),
        fp_frame=exc_return.fp_frame,
    )
    notes: list[str] = []
    if exc_return.secure_stack is not None:
        notes.append(
            f"EXC_RETURN says the frame is on a {'Secure' if exc_return.secure_stack else 'Non-secure'} "
            "stack. MSP and PSP here are the current security state's, so if the "
            "core is in the other state the frame is on the other bank's stack."
        )
    try:
        words = probe.read_memory(frame_address, 32)
        fault_words = (
            probe.read_memory(faults_mod.FAULT_REGION_BASE, faults_mod.FAULT_REGION_SIZE)
            if faults_mod.has_fault_registers(info.cpu) else {}
        )
    except Exception as exc:  # noqa: BLE001
        return failed(f"Reading the frame at 0x{frame_address:08X} failed: {exc}", **base)

    frame = {
        name: words.get(frame_address + 4 * i)
        for i, name in enumerate(faults_mod.FRAME_WORDS)
    }
    if any(value is None for value in frame.values()):
        return failed(
            f"The read at 0x{frame_address:08X} did not return all eight frame words.",
            **base,
        )
    base["frame"] = {name: f"0x{value:08X}" for name, value in frame.items()}
    base["faulting_pc"] = _hex(frame["PC"])
    base["stacked_lr"] = _hex(frame["LR"])

    if not faults_mod.missing_registers(fault_words):
        status = faults_mod.decode(fault_words)
        if any(bit.name == "IMPRECISERR" for bit in status.faults):
            notes.append(
                "The fault registers record an imprecise bus error. The stacked PC "
                "is then some instructions after the store that failed, not the "
                "store itself."
            )

    problems = faults_mod.frame_problems(frame)
    if elf is not None:
        try:
            segments = image_mod.load_segments(elf.read_bytes())
        except (OSError, image_mod.ImageError) as exc:
            return failed(f"{elf.name}: {exc}", **base)
        if not any(seg.vaddr <= frame["PC"] < seg.vaddr + seg.memsz for seg in segments):
            problems.append(f"the stacked PC is outside every segment of {elf.name}")
        base["faulting_function"] = _symbol(toolchain, elf, frame["PC"])
        instruction = _instruction(toolchain, elf, frame["PC"] & ~1)
        if instruction:
            base["faulting_function"]["instruction"] = instruction
        base["stacked_lr_function"] = _symbol(toolchain, elf, frame["LR"] & ~1)
    if notes:
        base["notes"] = notes

    if problems:
        return unverified(
            f"The words at 0x{frame_address:08X} do not look like an exception frame: "
            + "; ".join(problems) + ". A handler that pushes registers before this "
            "read moves the stack pointer below the frame, which then sits higher "
            "on the stack.",
            **base,
        )
    return verified(
        f"the exception frame at 0x{frame_address:08X} on {exc_return.stack}, found "
        f"from EXC_RETURN 0x{lr:08X} in LR; its stacked XPSR has the Thumb bit set",
        **base,
    )


def _symbol(toolchain: Toolchain, elf: Path, address: int) -> dict[str, Any]:
    """The function and source line holding `address`, from the ELF's symbols.

    None for either when the ELF does not say: a stripped image, or an address
    outside it, gives addr2line's "??"."""
    try:
        result = run_command(
            [str(toolchain.addr2line), "-f", "-C", "-e", str(elf), f"0x{address:08X}"],
            timeout=30,
        )
        lines = result.stdout.strip().splitlines()
    except ToolchainError as exc:
        return {"address": _hex(address), "error": str(exc)}
    function = lines[0].strip() if lines else "??"
    location = (lines[1].strip() if len(lines) > 1 else "??").split(" (discriminator")[0]
    file, _, line = location.rpartition(":")
    known = not location.startswith("??") and line.isdigit()
    # The shape run_until and core_registers give a place in the source.
    return {
        "function": None if function == "??" else function,
        "file": file if known else None,
        "line": int(line) if known else None,
        "address": _hex(address),
    }


_OBJDUMP_LINE = re.compile(r"^\s*([0-9a-f]+):\s+(?:[0-9a-f]{4}\s?)+\s*\t(.+)$", re.M)


def _instruction(toolchain: Toolchain, elf: Path, address: int) -> str | None:
    """The instruction at `address` as the ELF's code disassembles, or None.

    Agents decoded the faulting instruction by hand from read words in four of
    seven tasks of an evaluation, and one tried to run a disassembler it did not
    have (2026-10-04); the toolchain has objdump."""
    try:
        result = run_command([str(toolchain.objdump), "-d", f"--start-address=0x{address:08X}",
                              f"--stop-address=0x{address + 4:08X}", str(elf)], timeout=30)
    except ToolchainError:
        return None
    for match in _OBJDUMP_LINE.finditer(result.stdout):
        if int(match.group(1), 16) == address:
            return " ".join(match.group(2).split())
    return None


@mcp.tool()
@_serialised
def disassemble(
    elf_path: str,
    location: str,
    probe_serial: str | None = None,
    interface: Interface = "swd",
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Disassemble a function, or the code around an address, from the ELF, with
each instruction's source file and line and the function an inlined copy comes
from. `location` is a function name or an address. Without `probe_serial` it
reads only the ELF; with it, it also compares the target's code with the ELF's,
without halting, and is verified only when they match. At most 256
instructions; `cut` says when there were more.
    """
    elf = Path(elf_path).expanduser()
    if not elf.is_file():
        return failed(f"No such ELF: {elf}")
    try:
        toolchain = _toolchain()
    except ToolchainError as exc:
        return failed(str(exc))
    try:
        table = symbols_mod.functions(str(elf))
    except symbols_mod.SymbolError as exc:
        return failed(str(exc), elf_path=str(elf))
    at: int | None = None
    if re.fullmatch(r"0x[0-9A-Fa-f]+|\d+", location.strip()):
        try:
            at = _address(location.strip(), "location") & ~1
        except ValueError as exc:
            return failed(str(exc), elf_path=str(elf))
        holding = {(f.start, f.size): f for f in table if f.start <= at < f.start + f.size}
        if holding:
            chosen = min(holding.values(), key=lambda f: f.size)
            start, end = chosen.start, chosen.start + chosen.size
        else:
            chosen, start, end = None, max(at - 32, 0), at + 32
    else:
        name = location.strip()
        matches = {(f.start, f.size): f for f in table if f.name == name}
        if not matches:
            near = difflib.get_close_matches(name, sorted({f.name for f in table}), n=5)
            return failed(f"No function named {name} in {elf.name}'s symbol table."
                          + (f" Close names: {', '.join(near)}." if near else ""), elf_path=str(elf))
        if len(matches) > 1:
            listing = "; ".join(f"0x{f.start:08X}" + (f" in {f.file}" if f.file else "")
                                for f in sorted(matches.values(), key=lambda f: f.start))
            return failed(f"{name} names {len(matches)} functions: {listing}. Ask for the one you mean "
                          "by address.", elf_path=str(elf))
        chosen = next(iter(matches.values()))
        start, end = chosen.start, chosen.start + chosen.size
    stop = min(end, start + _DISASSEMBLY_SPAN)
    instructions = disasm_mod.run(toolchain, elf, start, stop)
    if not instructions:
        return failed(f"objdump found no code at {location} in {elf.name}: an address outside the ELF's "
                      "code.", elf_path=str(elf))
    function = chosen.name if chosen else None
    whole = len(instructions)
    first = 0
    if whole > disasm_mod.LIMIT and at is not None:
        # The asked address with some of what led to it, as far as the function allows.
        here = next((n for n, i in enumerate(instructions) if at < i.address + len(i.code)), whole - 1)
        first = max(0, min(here - 32, whole - disasm_mod.LIMIT))
    instructions = instructions[first:first + disasm_mod.LIMIT]
    cut = whole > disasm_mod.LIMIT or stop < end
    detail: dict[str, Any] = {
        "elf_path": str(elf),
        "function": function,
        **({"at": f"0x{at:08X}"} if at is not None else {}),
        **({"outside_functions": "No function in the symbol table holds this address, so these are the "
            "64 bytes around it, and the first may be decoded from the middle of an instruction."}
           if chosen is None else {}),
        "instructions": [
            {"address": f"0x{i.address:08X}", "instruction": i.text,
             **({"function": i.function} if i.function and i.function != function else {}),
             "file": i.file, "line": i.line,
             **({"here": True} if at is not None and i.address <= at < i.address + len(i.code) else {})}
            for i in instructions
        ],
        **({"cut": (f"{len(instructions)} of the function's {whole}{' or more' if stop < end else ''} "
                    f"instructions, 0x{instructions[0].address:08X} to 0x{instructions[-1].address:08X}; "
                    "ask by an address outside those for another part")} if cut else {}),
    }
    if probe_serial is None:
        return verified("disassembled from the ELF by objdump; not compared with the target's code", **detail)

    compared = _compare_with_target(toolchain, probe_serial, interface, instructions)
    if "error" in compared:
        return failed(compared.pop("error"), **compared, **detail)
    detail.update(compared)
    if compared["target_matches"]:
        return verified(f"disassembled from the ELF, and the target's {compared['bytes_compared']} bytes of this "
                        "code, read without halting it, are the ELF's", **detail)
    return unverified(f"The target's code differs from the ELF's at {compared['first_difference']}: it is not "
                      "running this ELF there, so these instructions are not what it executes.", **detail)


#: The most of one function objdump is asked for; a function longer than this
#: (64 KB) is a table, not code anybody reads by instruction.
_DISASSEMBLY_SPAN = 0x10000


def _compare_with_target(toolchain: Toolchain, probe_serial: str, interface: str,
                         instructions: list[Any]) -> dict[str, Any]:
    """The target's bytes over the instructions' range against the ELF's, read
    without halting it and beside a held server; at most 1024 bytes."""
    serial, _, error = _resolve_probe(toolchain, probe_serial)
    if error:
        return {"error": error}
    refusal = _held_refusal(serial)
    if refusal:
        return {"error": refusal["error"], "probe_serial": serial}
    expected = {i.address + n: b for i in instructions for n, b in enumerate(i.code)}
    start = min(expected) & ~3
    end = min((max(expected) + 4) & ~3, start + 1024)
    probe = Probe(toolchain, serial=serial, interface=interface, try_first=_settled_port(serial),
                  shared=_shares(serial))
    try:
        info = probe.identify(allow_reset=False)
        _remember_port(serial, probe, info)
        if not info.connected:
            return {"error": "Could not reach the core without resetting it, and this tool does not reset.",
                    "probe_serial": serial, "hint": explain_connect_failure(info, interface)}
        words = probe.read_memory(start, end - start)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "probe_serial": serial}
    target = {a + n: (w >> (8 * n)) & 0xFF for a, w in words.items() for n in range(4)}
    compared = [a for a in sorted(expected) if start <= a < end]
    differ = next((a for a in compared if target.get(a) != expected[a]), None)
    out: dict[str, Any] = {"probe_serial": serial, "bytes_compared": len(compared),
                           "target_matches": differ is None, "target_was_reset": probe.target_was_reset}
    if differ is not None:
        out["first_difference"] = f"0x{differ:08X}"
    if end - start < (max(expected) + 1) - start:
        out["compared_note"] = "Only the first 1024 bytes were compared."
    return out


@mcp.tool()
@_serialised
def capture_console(
    seconds: float = 5.0,
    probe_serial: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Capture what a target prints on its serial console for up to 60 seconds.

The port is the one the probe list maps to this probe's serial, or the one
STLINK_MCP_CONSOLE names for it, never the first /dev/ttyACM*; refused when
there is none. What the probe buffered before the port opened comes back
separately, in `console_buffered`. Hearing nothing is unverified: silence
cannot be told from a port not wired to the target.
    """
    too_long = _capture_seconds_problem(seconds, "seconds")
    if too_long:
        return failed(too_long)
    try:
        toolchain = _toolchain()
    except ToolchainError as exc:
        return failed(str(exc))

    serial, probe_info, error = _resolve_probe(toolchain, probe_serial, console=True)
    if error:
        return failed(error)

    port = probe_info.console_port if probe_info else None
    if port is None:
        return failed(
            f"No serial console was found for probe {serial}: neither the probe "
            "list nor udev's port names give one. If the serial is wrong, "
            "list_probes shows the attached probes. Otherwise name one with "
            f"{settings.CONSOLE}=<probe serial>=<device path>. This does not fall "
            "back to the first /dev/ttyACM*, because that path belongs to "
            "whichever board enumerated first, not to this one.",
            probe_serial=serial,
            **({"console_note": probe_info.console_note}
               if probe_info and probe_info.console_note else {}),
        )

    capture_error: str | None = None
    buffered = ""
    try:
        with console_mod.ConsoleCapture(port, settings.console_baud()) as cap:
            buffered = cap.buffered
            try:
                text = cap.collect(seconds)
            except Exception as exc:  # noqa: BLE001
                # A USB replug mid-capture raises SerialException, an OSError
                # rather than a ConsoleError, and used to escape the tool. Keep
                # what arrived, as run_firmware does, and say it was cut short.
                text, capture_error = cap.text, str(exc)
    except (console_mod.ConsoleError, settings.SettingError) as exc:
        return failed(str(exc), probe_serial=serial, console_port=port)

    detail: dict[str, Any] = {
        "probe_serial": serial,
        "console_port": port,
        "console_port_source": probe_info.console_source if probe_info else None,
        "seconds": seconds,
        "console": text,
        **_garbled_note(text),
        **_buffered_disclosure(buffered, before="this capture started", source=(
            "so it may be older, and it is not in `console`")),
    }
    source = (
        f"the port {settings.CONSOLE} assigns to this probe"
        if probe_info and probe_info.console_source == settings.CONSOLE
        else "the port mapped to this probe's serial"
    )
    if capture_error is not None:
        detail["console_error"] = capture_error
        if text.strip():
            return verified(
                f"partial serial console read from {source}, before the capture "
                "was interrupted",
                **detail,
            )
        return failed(f"The console could not be read: {capture_error}", **detail)

    if not text:
        # Silence on the port is not silence from the target: an adapter not
        # yet wired to the TX pin reads exactly the same.
        return unverified(
            f"Nothing arrived on {source} in {seconds:g} s. That does not "
            "show the target is silent: it may not be running or may not print, "
            f"it may print at a baud rate other than {settings.console_baud()}, "
            "or this port may not be wired to its TX pin.",
            **detail,
        )
    return verified(f"serial console read from {source}", **detail)


#: What a capture leaves behind on the target, said in every capture_swo result
#: (N4). Measured on an STM32F407, 2026-10-02: from power-up, the capture set
#: TRACE_IOEN in DBGMCU_CR (0x20), ITM's TCR and TER, and the TPIU, and after
#: reset_target DBGMCU_CR still read 0x20 while trace enable had been cleared.
_TRACE_NOTE = (
    "Trace output stays on until a power cycle, reset_target included (measured on an "
    "STM32F407): the SWO pin now belongs to the trace unit, not the application."
)


def _swo_garbled_note(text: str, clock_hz: int) -> dict[str, Any]:
    """A note when more than a tenth of an SWO capture is not text. Four clean
    captures at the right clock, on an F407 and an H7 (2026-10-02), had no
    such character in 1,922; a clock 6% off gave nothing but U+FFFD, and one
    a little further off about half. The console's threshold, half, would
    miss the second."""
    if len(text) < 16:
        return {}
    unreadable = sum(1 for ch in text if not (ch.isascii() and (ch.isprintable() or ch in "\r\n\t")))
    if unreadable * 10 <= len(text):
        return {}
    return {"swo_garbled_note": (
        f"{unreadable} of {len(text)} characters are not readable text, which is "
        f"what a core clock other than the {clock_hz} Hz given looks like: the "
        "SWO rate is derived from it. Give the clock the core runs at while it "
        "writes, which may differ from its clock at reset."
    )}


@mcp.tool()
@_serialised
def capture_swo(
    core_clock_hz: int,
    seconds: float = 5.0,
    itm_port: int = 0,
    probe_serial: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Capture what the target writes to an ITM stimulus port, over the SWO pin.

`core_clock_hz` is the clock the core runs at while it writes; a wrong one
gives nothing or garbled text. Does not halt or
reset, but turns trace output on and leaves it on, taking the SWO pin from the
application until a power cycle. SWD only. Works beside a GDB server holding the
probe when stlink-server is installed. Hearing nothing is unverified.
    """
    too_long = _capture_seconds_problem(seconds, "seconds")
    if too_long:
        return failed(too_long)
    if not 0 <= itm_port <= 31:
        return failed(f"itm_port is {itm_port}; ITM has stimulus ports 0 to 31.")
    if core_clock_hz <= 0:
        return failed(f"core_clock_hz is {core_clock_hz}; give the core's clock in Hz.")

    probe: Probe | None = None
    try:
        toolchain = _toolchain()
        serial, _, error = _resolve_probe(toolchain, probe_serial)
        if error:
            return failed(error)
        refusal = _held_refusal(serial)
        if refusal:
            return refusal
        probe = Probe(toolchain, serial=serial, try_first=_settled_port(serial), shared=_shares(serial))
        info = probe.identify(allow_reset=False)
        _remember_port(serial, probe, info)
        if not info.connected:
            return failed(
                "Could not reach the core without resetting it, and this tool "
                "does not reset.",
                probe_serial=serial,
                **_connection_fields(probe, info),
                hint=explain_connect_failure(info),
            )
        text = probe.capture_swo(core_clock_hz, seconds, itm_port)
    except Exception as exc:  # noqa: BLE001
        # CS2: the programmer may have set up the trace unit before failing.
        return failed(
            str(exc), **_after_failure(probe), trace_left_enabled="possibly",
            trace_note="The programmer may have turned trace output on before this failed. " + _TRACE_NOTE,
        )

    detail: dict[str, Any] = {
        "probe_serial": serial,
        **_connection_fields(probe, info),
        "core_clock_hz": core_clock_hz,
        "itm_port": itm_port,
        "seconds": seconds,
        "target_was_reset": probe.target_was_reset,
        "swo": text,
        **_swo_garbled_note(text, core_clock_hz),
        "trace_left_enabled": True,
        "trace_note": _TRACE_NOTE,
    }
    if not text:
        return unverified(
            f"Nothing arrived on ITM port {itm_port} over SWO in {seconds:g} s. "
            "That does not show the target is silent: it may not write to that "
            f"port, its core may not run at the {core_clock_hz} Hz given (twice "
            "the real clock gave nothing at all on an STM32F407), or this board "
            "may not have its SWO pin wired to the probe.",
            **detail,
        )
    return verified(f"ITM port {itm_port} output received over SWO", **detail)


def _connection_fields(probe: Probe, info: TargetInfo | None) -> dict[str, Any]:
    """The interface asked for, the clock the connect banner reported for it,
    and the warning when the access port in use was never measured.

    The banner's "SWD freq" or "JTAG freq" line is the observation. A clock of
    None means the banner did not say, which a failed connect can produce. The
    port warning used to appear in board_state alone, so the tool that then
    failed to halt the core on that port said nothing about it (red-team RT7,
    2026-09-03)."""
    fields: dict[str, Any] = {
        "interface": probe.interface,
        "interface_clock_khz": info.interface_khz if info else None,
    }
    fields.update(_port_warning(probe))
    if info is not None and info.interface and info.interface != probe.interface:
        fields["interface_note"] = (
            f"Asked for {probe.interface.upper()}, and the connect banner reported "
            f"{info.interface.upper()}."
        )
    return fields


def _port_warning(probe: Probe | None) -> dict[str, Any]:
    if probe is None or not probe.unmeasured_port_warning:
        return {}
    return {"access_port_warning": probe.unmeasured_port_warning}


def _after_failure(probe: Probe | None) -> dict[str, Any]:
    """What a failure after identify() still owes the agent: whether the target
    was reset getting there, and whether the port it used was a guess."""
    if probe is None:
        return {}
    return {"target_was_reset": probe.target_was_reset, **_port_warning(probe)}


def _hex(value: int | None) -> str | None:
    return None if value is None else f"0x{value:08X}"
