"""Loading an ELF into target RAM and running it, with verification.

The flow is: a GDB server that resets and halts the core, then GDB writes the
ELF sections, sets the stack pointer and program counter from the image's own
vector table, and detaches so the target runs on.

Two details are load-bearing and both are easy to get wrong:

  access port   The server must be told which one. On a family that does not use
                AP0 the failure is "Failed to halt target", which reads like
                broken silicon rather than a wrong flag.

  reset+halt    Connecting without resetting leaves any previously running
                program in place. GDB cannot set the PC of a running thread, so
                the load appears to succeed and the old program simply carries
                on. The symptom is output that looks right but never restarts.

SP and PC come from the loaded image's vector table rather than from the ELF
entry point, so nothing has to be kept in sync as the image grows.

RESET BEFORE EVERY LOAD. This is not optional and it is not the same as the
server's own --halt, which resets only once when the server starts. Holding a
server across runs means the second and subsequent images inherit the previous
run's LIVE state: peripherals configured, NVIC lines still enabled, and any
external device still asserting an interrupt. The new image then starts at
Reset_Handler with that interrupt pending, vectors through a handler pointer in
.bss that has not been zeroed yet, and HardFaults during start-up.

Measured 2026-08-15: without this, every run after the first through a held
session faulted, with IPSR reading 3 (HardFault) and SysTick pending but never
serviced. It cost an evening, because the symptom is a target that prints its
first two bytes and then stops, which reads as a firmware bug rather than a
debugger one.
"""

from __future__ import annotations

import atexit
import collections
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .toolchain import Toolchain, ToolchainError, run_command, strip_ansi


class RunError(RuntimeError):
    pass


def port_is_taken(port: int, host: str = "") -> bool:
    """Whether anything already holds this port.

    Tested by trying to BIND it, never by connecting to it. Connecting is wrong
    twice over. It opens a real client connection to whatever is there, and for
    a GDB server that means touching a live debug session in order to ask
    whether it exists. And it is unreliable in the direction that costs most: a
    busy server that does not accept within the timeout reads as absent.

    Measured 2026-08-15, with four held gdbservers on this machine. The previous
    connect-based check, with a 0.25 s timeout, reported four of their eight
    ports free while `ss -ltnp` listed every one of them listening. Port
    allocation built on that would have handed a second server a port already in
    use, and reported the resulting failure as a fault of the board.

    Binds the wildcard address by default, so a server bound to 0.0.0.0 and one
    bound only to loopback are both detected.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return True
        return False


def port_is_accepting(port: int, host: str = "127.0.0.1") -> bool:
    """Whether a server on this port is up and taking connections yet.

    A different question from port_is_taken, and it needs the opposite
    instrument. Waiting for our OWN server to come up cannot use the bind test,
    because that test binds the port for an instant and would race the child we
    are waiting for, occasionally stealing the port from it. Connecting is
    correct here: the connection is to a server we started, and its purpose is
    exactly to find out whether it is ready.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex((host, port)) == 0


class GdbServer:
    """A GDB server process.

    Prefer `acquire_server()` over constructing this directly: on some parts the
    server must be kept alive across runs, and that policy lives there.
    """

    def __init__(
        self,
        toolchain: Toolchain,
        access_port: int,
        port: int = 61234,
        serial: str | None = None,
        interface: str = "swd",
    ) -> None:
        self.toolchain = toolchain
        self.interface = interface
        self.access_port = access_port
        self.port = port
        self.serial = serial
        self._proc: subprocess.Popen[str] | None = None
        # Bounded, and drained by a thread for the life of the server. The pipe
        # used to be created and then never read on the success path, which is
        # fine for a short-lived process and not fine here, because this server
        # is deliberately held across many runs. Once the OS pipe buffer fills
        # (65536 bytes on this machine, by F_GETPIPE_SZ) the server blocks
        # writing to it and simply stops, which would look like the probe or the
        # target failing. Measured rate is a 370-byte banner plus 54 bytes per
        # connect/detach cycle, with nothing periodic, so the old code had well
        # over a thousand cycles of headroom. That is a reason it was never hit,
        # not a reason it was safe.
        self._log: collections.deque[str] = collections.deque(maxlen=400)
        self._drain: threading.Thread | None = None

    def __enter__(self) -> "GdbServer":
        # Both ports, because the server opens -p and -p+1 (SWO). Checking only
        # the GDB port lets a collision surface later as an opaque failure from
        # the server itself rather than here, where the cause is obvious.
        for port in (self.port, self.port + 1):
            if port_is_taken(port):
                raise RunError(
                    f"Port {port} is already in use. A GDB server instance needs "
                    f"{self.port} and {self.port + 1} (the second is SWO). Likely "
                    "another server, an IDE, too small a port stride between "
                    "boards, or a server orphaned by a previous run that was "
                    f"killed. `ss -ltnp | grep {port}` names the process holding "
                    "it, and its command line carries the probe serial."
                )

        argv = [
            str(self.toolchain.gdb_server),
            # -d selects SWD and its absence JTAG; the server has no JTAG flag.
            # With -d its option list shows "SWD Debug : Enabled", without it
            # nothing (measured 2026-10-01, ST-LINK GDB server 7.14.0).
            *(["-d"] if self.interface == "swd" else []),
            "-e",                      # persistent; without it the server drops
                                       # the GDB client on connect with
                                       # "Target disconnected: Connection reset
                                       # by peer", which reads like a target
                                       # fault rather than a missing flag
            "-p", str(self.port),
            "-z", str(self.port + 1),   # SWO; explicit so the pair is documented
                                        # rather than relying on the +1 default
            "--apid", str(self.access_port),
            "--halt",                  # reset and halt, so every run starts clean
            "-cp", str(self.toolchain.programmer_bin_dir),
        ]
        if self.serial:
            argv += ["-i", self.serial]
        self._proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )

        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if port_is_accepting(self.port):
                self._start_draining()
                return self
            if self._proc.poll() is not None:
                out = strip_ansi(self._proc.stdout.read() if self._proc.stdout else "")
                raise RunError(f"GDB server exited before listening:\n{out.strip()}")
            time.sleep(0.2)

        self.__exit__()
        raise RunError(f"GDB server did not start listening on port {self.port}")

    def _start_draining(self) -> None:
        """Keep reading the server's stdout so it can never block writing to it.

        A daemon thread, because it must not keep the interpreter alive, and
        because the read is blocking. It ends by itself when the pipe closes.
        """
        if self._proc is None or self._proc.stdout is None:
            return

        stream = self._proc.stdout

        def pump() -> None:
            try:
                for line in stream:
                    self._log.append(strip_ansi(line.rstrip("\n")))
            except (ValueError, OSError):
                # Closed under us by __exit__. Not an error worth surfacing.
                pass

        self._drain = threading.Thread(
            target=pump, name=f"gdbserver-drain-{self.port}", daemon=True
        )
        self._drain.start()

    def recent_output(self, lines: int = 20) -> list[str]:
        """The last thing the server said, for diagnosing why it stopped."""
        return list(self._log)[-lines:]

    def is_alive(self) -> bool:
        """Whether this server is still running AND still serving.

        Both are checked, because they fail apart: the process can exit while
        the port lingers, and it can survive while no longer listening. A held
        session that has quietly died is the worst state this tool can be in,
        so the test is deliberately the strict one.
        """
        if self._proc is None or self._proc.poll() is not None:
            return False
        return port_is_taken(self.port)

    def __exit__(self, *exc_info: object) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=5)
        self._proc = None


# ---------------------------------------------------------------------------
# Server lifetime
#
# The server is kept alive across runs, deliberately, and this is the single most
# useful thing measured about the STM32N6.
#
# Once a TYPICAL image has run, the debug access port cannot be RE-acquired: a
# fresh connect fails with "Cannot connect to access port 1", and neither a
# software nor a hardware reset clears it. Only removing power does. But an
# already established server connection keeps working indefinitely: consecutive
# load-and-run cycles through one server all restart cleanly, where tearing the
# server down in between needs a power cycle. So holding the server turns a power
# cycle per run into a power cycle per session.
#
# "Typical image" is doing real work in that sentence. This is not a property of
# the silicon: on the STM32N6 it is caused by ST's SystemInit gating the SYSCFG
# clock on its way out, and an image that leaves that clock on keeps the port.
# See quirks.py for the line. Holding the server is still worth doing, because
# it is faster and because most images out there do gate it, but it is a
# workaround for someone else's boilerplate rather than a hardware constraint.
#
# The cost is real and is surfaced rather than hidden: the server holds the probe,
# so the STM32_Programmer_CLI-based tools cannot run while it is held, and
# releasing it to use them forfeits the ability to run firmware again until the
# board is power cycled. Callers are told that instead of getting a bare
# "Device connect error".
# ---------------------------------------------------------------------------

# One server per probe, keyed by serial. A multi-board rig needs a server per
# board, each on its own port; a single global session would silently hand back a
# server attached to the wrong board and load firmware onto it while reporting
# success.
_sessions: dict[str | None, GdbServer] = {}
_atexit_registered = False


def prune_dead_servers() -> list[str | None]:
    """Forget sessions whose server has died. Returns the serials affected.

    A dead session left in the registry is worse than no session at all. Reads
    are refused because a server is "held", while every load connects to a
    closed port; GDB in -batch then aborts its script, so nothing is written and
    the previous image simply carries on running and printing. That combination
    reads as a working board.
    """
    dead = [serial for serial, server in _sessions.items() if not server.is_alive()]
    for serial in dead:
        _sessions[serial].__exit__()
        _sessions.pop(serial, None)
    return dead


def acquire_server(
    toolchain: Toolchain,
    access_port: int,
    port: int = 61234,
    serial: str | None = None,
    interface: str = "swd",
) -> GdbServer:
    """The live server for one probe, starting it if needed."""
    global _atexit_registered

    prune_dead_servers()

    existing = _sessions.get(serial)
    if existing is not None:
        if existing.interface != interface:
            raise RunError(
                f"The GDB server holding this probe uses {existing.interface.upper()}, "
                f"and this call asked for {interface.upper()}. Reusing it would run "
                "over the interface you did not ask for. Call release_probe() first, "
                "or pass the interface the server uses."
            )
        return existing

    # A held server must not outlive this process. If it does it keeps holding
    # the probe, and every later tool fails with a bare "Device connect error"
    # naming neither the cause nor the orphan. Found the hard way immediately
    # after introducing the held server.
    #
    # atexit covers a normal exit and SIGINT, and nothing else: CPython installs
    # no SIGTERM handler, so `kill` and any supervisor bypass this entirely.
    # __main__ installs handlers for SIGTERM and SIGHUP to close that gap.
    # SIGKILL is not coverable by anything in-process, so an orphan is still
    # possible and port_is_taken below is what surfaces it.
    if not _atexit_registered:
        atexit.register(release_all_servers)
        _atexit_registered = True

    if any(existing.port == port for existing in _sessions.values()):
        raise RunError(
            f"Port {port} is already used by a server for another probe. "
            "Give each board its own GDB port."
        )

    server = GdbServer(toolchain, access_port, port, serial=serial, interface=interface)
    server.__enter__()
    _sessions[serial] = server
    return server


def server_is_held(serial: str | None = None) -> bool:
    """Whether a LIVE server is held. With no serial, whether any is, since the
    programmer CLI is blocked by any of them.

    Dead sessions are pruned first rather than counted. Membership of the
    registry used to be the whole test, which meant a server that had died still
    blocked every read with "a GDB server is holding the probe" while no longer
    holding anything.
    """
    prune_dead_servers()
    if serial is None:
        return bool(_sessions)
    return serial in _sessions


def held_serials() -> list[str | None]:
    prune_dead_servers()
    return list(_sessions)


def release_server(serial: str | None = None) -> bool:
    """Shut down one probe's server, or all of them when given no serial.

    Where a typical image closes the access port this is a one-way door until
    the board is power cycled, so it is never done implicitly. See quirks.py:
    that is caused by the image, not by the part, and is removable.
    """
    if serial is None:
        return release_all_servers()
    server = _sessions.pop(serial, None)
    if server is None:
        return False
    server.__exit__()
    return True


def release_all_servers() -> bool:
    released = False
    for server in list(_sessions.values()):
        server.__exit__()
        released = True
    _sessions.clear()
    return released


_GDB_SCRIPT = """\
set confirm off
set pagination off
target extended-remote localhost:{port}
monitor reset
load
set $sp = *(unsigned int *){vector:#x}
set $pc = (*(unsigned int *)({vector:#x} + 4)) & ~1
printf "STLINK_MCP_SP=0x%08x\\n", $sp
printf "STLINK_MCP_PC=0x%08x\\n", $pc
detach
"""


def load_and_run(
    toolchain: Toolchain,
    elf: Path,
    vector_table_addr: int,
    access_port: int,
    gdb_port: int = 61234,
) -> tuple[int | None, int | None, int | None, str]:
    """Load the ELF into RAM and start it. Returns (sp, pc, load_size, output)."""
    if not elf.exists():
        raise RunError(f"No such ELF: {elf}")

    script = _GDB_SCRIPT.format(port=gdb_port, vector=vector_table_addr)
    with tempfile.NamedTemporaryFile("w", suffix=".gdb", delete=False) as handle:
        handle.write(script)
        script_path = Path(handle.name)

    try:
        result = run_command(
            [str(toolchain.gdb), "-q", "-batch", "-x", str(script_path), str(elf)],
            timeout=180,
        )
    finally:
        script_path.unlink(missing_ok=True)

    output = result.stdout

    def _grab(marker: str) -> int | None:
        for line in output.splitlines():
            if line.startswith(marker):
                return int(line.split("=", 1)[1].strip(), 16)
        return None

    # Informational only, so a line GDB words differently yields None rather
    # than a ValueError that would escape run_firmware with the server held.
    load_size = None
    for line in output.splitlines():
        if line.startswith("Start address") and "load size" in line:
            try:
                load_size = int(line.rsplit("load size", 1)[1].strip().rstrip(","))
            except ValueError:
                load_size = None

    return _grab("STLINK_MCP_SP="), _grab("STLINK_MCP_PC="), load_size, output


# entry_point() lived here: it read the ELF header with readelf to find the
# entry address. Nothing called it, because the GDB script takes both SP and PC
# straight out of the image's vector table instead, which needs no extra tool.
# It was removed rather than kept "in case", since its only lasting effect was
# that Toolchain.check() refused to start without arm-none-eabi-readelf.
