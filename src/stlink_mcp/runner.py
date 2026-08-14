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
"""

from __future__ import annotations

import atexit
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .toolchain import Toolchain, ToolchainError, run_command, strip_ansi


class RunError(RuntimeError):
    pass


def _port_is_listening(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex((host, port)) == 0


@dataclass
class RunResult:
    ok: bool
    stack_pointer: int | None
    program_counter: int | None
    load_size: int | None
    console_output: str
    detail: str

    @property
    def verified_by(self) -> str:
        return "console output" if self.console_output.strip() else "none"


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
    ) -> None:
        self.toolchain = toolchain
        self.access_port = access_port
        self.port = port
        self.serial = serial
        self._proc: subprocess.Popen[str] | None = None
        self._log = ""

    def __enter__(self) -> "GdbServer":
        if _port_is_listening(self.port):
            raise RunError(
                f"Something is already listening on port {self.port}. "
                "Another GDB server or an IDE is probably holding the probe."
            )

        argv = [
            str(self.toolchain.gdb_server),
            "-d",                      # SWD
            "-e",                      # persistent; without it the server drops
                                       # the GDB client on connect with
                                       # "Target disconnected: Connection reset
                                       # by peer", which reads like a target
                                       # fault rather than a missing flag
            "-p", str(self.port),
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
            if _port_is_listening(self.port):
                return self
            if self._proc.poll() is not None:
                out = strip_ansi(self._proc.stdout.read() if self._proc.stdout else "")
                raise RunError(f"GDB server exited before listening:\n{out.strip()}")
            time.sleep(0.2)

        self.__exit__()
        raise RunError(f"GDB server did not start listening on port {self.port}")

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
# Once firmware has run, the debug access port cannot be RE-acquired: a fresh
# connect fails with "Cannot connect to access port 1", and neither a software
# nor a hardware reset clears it. Only removing power does. But an already
# established server connection keeps working indefinitely: two consecutive
# load-and-run cycles through one server both restart cleanly, where tearing the
# server down in between needs a power cycle. So holding the server turns a power
# cycle per run into a power cycle per session.
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


def acquire_server(
    toolchain: Toolchain,
    access_port: int,
    port: int = 61234,
    serial: str | None = None,
) -> GdbServer:
    """The live server for one probe, starting it if needed."""
    global _atexit_registered

    existing = _sessions.get(serial)
    if existing is not None:
        return existing

    # A held server must not outlive this process. If it does it keeps holding
    # the probe, and every later tool fails with a bare "Device connect error"
    # naming neither the cause nor the orphan. Found the hard way immediately
    # after introducing the held server.
    if not _atexit_registered:
        atexit.register(release_all_servers)
        _atexit_registered = True

    if any(existing.port == port for existing in _sessions.values()):
        raise RunError(
            f"Port {port} is already used by a server for another probe. "
            "Give each board its own GDB port."
        )

    server = GdbServer(toolchain, access_port, port, serial=serial)
    server.__enter__()
    _sessions[serial] = server
    return server


def server_is_held(serial: str | None = None) -> bool:
    """Whether a server is held. With no serial, whether ANY is held, since the
    programmer CLI is blocked by any of them."""
    if serial is None:
        return bool(_sessions)
    return serial in _sessions


def held_serials() -> list[str | None]:
    return list(_sessions)


def release_server(serial: str | None = None) -> bool:
    """Shut down one probe's server, or all of them when given no serial.

    On parts where the access port cannot be re-acquired this is a one-way door
    until the board is power cycled, so it is never done implicitly.
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

    load_size = None
    for line in output.splitlines():
        if line.startswith("Start address") and "load size" in line:
            load_size = int(line.rsplit("load size", 1)[1].strip().rstrip(","))

    return _grab("STLINK_MCP_SP="), _grab("STLINK_MCP_PC="), load_size, output


def entry_point(toolchain: Toolchain, elf: Path) -> int | None:
    """The ELF's entry point, read from the file rather than assumed.

    Hardcoding this is a real trap: it is the reset handler's address and it
    moves whenever the image changes size, and starting at a stale address fails
    silently rather than loudly.
    """
    result = run_command([str(toolchain.readelf), "-h", str(elf)], timeout=60)
    for line in result.stdout.splitlines():
        if "Entry point address" in line:
            try:
                return int(line.split(":", 1)[1].strip(), 16)
            except ValueError:
                return None
    raise ToolchainError(f"Could not read an entry point from {elf}")
