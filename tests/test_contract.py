"""The contract every tool result must keep.

A red-team pass found the project's central rule, report success only after
checking the effect, broken in four tools at once and in four different ways:
a hardcoded ok, an ok derived from the wrong evidence, an ok served from a
cache, and an ok sitting next to `verified_by: "nothing"`. Fixing those four
would not have been enough, because nothing stopped a fifth appearing.

These tests are what stops the fifth. The rule under test, stated once:

    No tool, under any input, returns ok=True without naming the evidence that
    established it.

Nothing here touches hardware. The board, the probe, the vendor CLI and the
serial port are all faked, which is the point: these are properties of the tool
surface, and they must hold on a machine with no STM32 attached at all.
"""

from __future__ import annotations

import re

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from stlink_mcp import probe as probe_mod
from stlink_mcp import faults, result, server
from stlink_mcp.toolchain import ToolchainError

# Every tool, with the smallest argument set that reaches its body.
TOOL_CALLS: dict[str, dict] = {
    "list_probes": {},
    "board_state": {},
    "run_firmware": {"elf_path": "/nonexistent.elf"},
    "release_probe": {},
    "flash_firmware": {"image_path": "/nonexistent.bin"},
    "read_memory": {"address": 0x20000000},
    "core_registers": {},
    "capture_console": {"seconds": 0.01},
    "capture_swo": {"core_clock_hz": 16_000_000, "seconds": 0.01},
    "reset_target": {},
    "read_fault_status": {},
    "locate_fault": {},
    "run_until": {"elf_path": "/nonexistent.elf", "location": "main"},
}


def call(name: str, **overrides):
    """Invoke a tool by name, past the MCP decorator."""
    fn = getattr(server, name)
    fn = getattr(fn, "fn", fn)
    args = dict(TOOL_CALLS[name])
    args.update(overrides)
    return fn(**args)


def assert_contract(r: object, where: str = "") -> None:
    """The whole rule, in one place."""
    assert isinstance(r, dict), f"{where}: result is not a dict"
    assert "ok" in r and "status" in r, f"{where}: missing ok/status"
    assert r["status"] in (result.VERIFIED, result.UNVERIFIED, result.FAILED)
    assert r["ok"] is (r["status"] == result.VERIFIED), (
        f"{where}: ok={r['ok']} disagrees with status={r['status']}"
    )
    if r["ok"]:
        evidence = (r.get("verified_by") or "").strip()
        assert evidence, f"{where}: ok=True with nothing to justify it"
        # "nothing" is a non-empty string, and was the shipped value here beside
        # ok=True. A truthiness check alone does not catch the case that matters.
        assert evidence.strip(".").casefold() not in result.NON_EVIDENCE, (
            f"{where}: ok=True with verified_by={evidence!r}, which asserts the "
            "absence of evidence"
        )
    else:
        assert r.get("error") or r.get("unverified_because"), (
            f"{where}: not ok, and no reason given"
        )


class TestTheConstructors:
    """result.py is where the rule is enforced, so it is tested first."""

    def test_success_will_not_build_without_evidence(self) -> None:
        with pytest.raises(result.EvidenceError):
            result.verified("")

    def test_whitespace_is_not_evidence(self) -> None:
        with pytest.raises(result.EvidenceError):
            result.verified("   \n ")

    @pytest.mark.parametrize("word", ["nothing", "none", "N/A", "Nothing.", "unknown"])
    def test_a_word_meaning_no_evidence_is_not_evidence(self, word: str) -> None:
        """The shape that actually shipped: verified_by "nothing" beside ok=True.

        A non-empty check passes this, because "nothing" is a non-empty string.
        That is how the rule was kept in letter while being broken in full, and
        it is why the constructor rejects the words rather than just the blank.
        """
        with pytest.raises(result.EvidenceError):
            result.verified(word)

    @pytest.mark.parametrize(
        "key", ["ok", "status", "verified_by", "unverified_because", "error"]
    )
    def test_a_caller_cannot_set_the_verdict_by_hand(self, key: str) -> None:
        """The loophole that would put us back where we started."""
        with pytest.raises(result.EvidenceError):
            result.verified("a real observation", **{key: "anything"})

    def test_unverified_is_not_a_success(self) -> None:
        r = result.unverified("nobody looked")
        assert r["ok"] is False
        assert r["unverified_because"] == "nobody looked"
        assert r["verified_by"] is None

    def test_unverified_must_say_why_it_could_not_check(self) -> None:
        with pytest.raises(result.EvidenceError):
            result.unverified("")

    def test_evidence_survives_into_the_result(self) -> None:
        r = result.verified("console output", probe_serial="AAA1")
        assert r["ok"] is True
        assert r["verified_by"] == "console output"
        assert r["probe_serial"] == "AAA1"


class TestEveryToolIsActuallyCovered:
    """The list above is hand-written, and a hand-written list of what to check
    is the oldest way to have an unchecked thing.

    Adding `reset_target` proved it: the whole suite stayed green while the new
    tool was covered by none of it, because every parametrised test iterates
    TOOL_CALLS rather than the server. Green meant "the eight tools I remembered
    are fine", which reads identically to "all tools are fine". That is the same
    shape as a check that cannot produce a positive, and this project has now
    hit that shape four times.
    """

    def test_no_registered_tool_escapes_the_contract_suite(self) -> None:
        registered = set(server.mcp._tool_manager._tools)
        missing = registered - set(TOOL_CALLS)
        assert not missing, (
            f"tools registered with the server but absent from TOOL_CALLS, so "
            f"nothing in this file tests them: {sorted(missing)}"
        )

    def test_the_list_does_not_name_tools_that_do_not_exist(self) -> None:
        """The other direction: a renamed tool leaving a stale entry behind
        would keep passing while testing nothing."""
        registered = set(server.mcp._tool_manager._tools)
        stale = set(TOOL_CALLS) - registered
        assert not stale, f"TOOL_CALLS names tools the server does not register: {sorted(stale)}"


class TestToolCallsShareOnlyWhatTheyMust:
    """The MCP SDK runs each request as its own task and each tool in a worker
    thread, so an agent's parallel calls really are parallel. Measured
    2026-10-01: commands on two probes at once ran clean, and a probe listing
    beside them broke some. So calls on different probes run together, calls on
    one probe queue, and anything that lists runs alone."""

    @pytest.mark.parametrize("name", sorted(TOOL_CALLS))
    def test_every_registered_tool_waits_while_the_gate_is_held(self, name: str, bench) -> None:
        """Through the function the SDK itself calls, so a new tool registered
        without @_serialised fails here rather than racing in use. Called both
        with and without a serial, which take the two sides of the gate."""
        import threading

        fn = server.mcp._tool_manager._tools[name].fn
        files = {"run_firmware": {"elf_path": str(bench.elf)},
                 "flash_firmware": {"image_path": str(bench.bin)}}
        variants = [{**TOOL_CALLS[name], **files.get(name, {})}]
        if "probe_serial" in inspect.signature(fn).parameters:
            variants.append({**variants[0], "probe_serial": "AAAA0001"})
        for args in variants:
            done = threading.Event()
            worker = threading.Thread(target=lambda: (fn(**args), done.set()))
            with server._GATE.exclusive():
                worker.start()
                assert not done.wait(0.2), f"{name}({args}) ran while the gate was held"
            worker.join(5)
            assert done.is_set(), f"{name}({args}) never ran after the gate was released"

    def _overlap(self, monkeypatch, calls: list[tuple[str, dict]]) -> int:
        """The most of these calls that were inside a probe read at once."""
        import threading
        import time

        active = peak = 0
        guard = threading.Lock()
        real = _FakeProbe.read_memory

        def slow_read(self, address, length_bytes):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(0.15)
            with guard:
                active -= 1
            return real(self, address, length_bytes)

        monkeypatch.setattr(_FakeProbe, "read_memory", slow_read)
        threads = [threading.Thread(target=call, args=(n,), kwargs=k) for n, k in calls]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        return peak

    def test_calls_on_different_probes_run_together(self, bench, monkeypatch) -> None:
        peak = self._overlap(monkeypatch, [
            ("read_memory", {"probe_serial": "AAAA0001"}),
            ("read_memory", {"probe_serial": "BBBB0002"}),
        ])
        assert peak == 2

    def test_calls_on_one_probe_queue(self, bench, monkeypatch) -> None:
        peak = self._overlap(monkeypatch, [
            ("read_memory", {"probe_serial": "AAAA0001"}),
            ("read_memory", {"probe_serial": "aaaa0001"}),
            ("read_fault_status", {"probe_serial": "AAAA0001"}),
        ])
        assert peak == 1

    def test_a_call_that_lists_runs_alone(self, bench, monkeypatch) -> None:
        peak = self._overlap(monkeypatch, [
            ("read_memory", {"probe_serial": "AAAA0001"}),
            ("read_memory", {}),  # no serial: it has to list the probes
            ("read_memory", {"probe_serial": "BBBB0002"}),
        ])
        assert peak <= 2
        # And the listing call itself never shared the gate:
        assert not server._GATE.held_exclusively_here()

    def test_listing_outside_the_exclusive_side_is_refused(self, bench) -> None:
        with server._GATE.shared():
            with pytest.raises(RuntimeError, match="exclusive side"):
                server._resolve_probe(object(), None)


class TestANamedProbeIsTheOneThatAnswers:
    """The programmer ignores an sn= it cannot parse as hex and connects to the
    first probe it finds (measured 2026-10-01: sn=NONE and sn=X answered from an
    ST-LINK/V2). With no listing in front of a named probe, that would read the
    wrong board and report it."""

    def test_a_banner_from_another_probe_is_refused(self, monkeypatch) -> None:
        from stlink_mcp import probe as pm
        from stlink_mcp.toolchain import CommandResult
        banner = "ST-LINK SN  : 53FF75065177485736340687\nDevice ID   : 0x450\n"
        monkeypatch.setattr(pm, "run_command", lambda argv, timeout=90: CommandResult(argv, 0, banner))
        p = pm.Probe(toolchain=SimpleNamespace(programmer="x"), serial="0054003D3137511139383538")
        with pytest.raises(pm.ProbeError, match="answered from probe 53FF75065177485736340687"):
            p.identify(allow_reset=False)

    def test_the_same_serial_in_another_case_is_the_same_probe(self, monkeypatch) -> None:
        from stlink_mcp import probe as pm
        from stlink_mcp.toolchain import CommandResult
        banner = "ST-LINK SN  : 0054003D3137511139383538\nDevice ID   : 0x450\n"
        monkeypatch.setattr(pm, "run_command", lambda argv, timeout=90: CommandResult(argv, 0, banner))
        p = pm.Probe(toolchain=SimpleNamespace(programmer="x"), serial="0054003d3137511139383538")
        assert p.identify(allow_reset=False).connected

    @pytest.mark.parametrize("serial", ["NONE", "X", "0054003D-31"])
    def test_a_serial_that_is_not_hex_is_refused_before_any_connect(self, serial, bench) -> None:
        out = call("read_memory", probe_serial=serial)
        assert_contract(out)
        assert out["status"] == result.FAILED and "not a probe serial" in out["error"]


class TestNoToolSetsOkByHand:
    def test_the_tool_surface_contains_no_literal_ok(self) -> None:
        """A grep, deliberately.

        Every defect this suite was written for looked reasonable in review. The
        cheapest guard against the next one is that the string does not appear:
        a tool that wants to set its own verdict has to work around this test,
        which is a conversation rather than an oversight.
        """
        source = Path(server.__file__).read_text()
        assert '"ok"' not in source, "a tool is setting ok directly again"


class _FakeProbe:
    """A board that always answers, so the tools' own logic is what is tested."""

    device_id = 0x486  # STM32N6, the family the fake board's AP1 answers below
    cpu: str | None = "Cortex-M55"  # what the captured N6 connect banner reports
    device_name: str | None = None
    board: str | None = None

    # Class-level so a test can patch them; __init__ must not overwrite them.
    access_port_provenance: str | None = "measured"
    other_ports_that_answered: tuple[int, ...] = ()
    unmeasured_port_warning: str | None = None

    def __init__(self, *args, **kwargs) -> None:
        self.interface = kwargs.get("interface", "swd")
        self.shared = kwargs.get("shared", False)
        self.connect_mode = "HOTPLUG"
        self.target_was_reset = False
        self.degraded_reason: str | None = None
        self.access_port = 1

    def identify(self, allow_reset: bool = True) -> probe_mod.TargetInfo:
        return probe_mod.TargetInfo(
            connected=True, device_id=self.device_id, cpu=self.cpu,
            device_name=self.device_name, board=self.board,
        )

    def gdb_access_port(self, info: object) -> int:
        return 1

    # What a non-halting read of DHCSR and the PC sampler gives: by default it
    # cannot be read, as on a sleeping core; a test sets a CoreState.
    core: object = None

    def core_state(self, samples: int = 5):
        if self.core is None:
            raise probe_mod.ProbeError("The core's debug status could not be read without halting it.")
        return self.core

    # Class-level so a test can lay out memory and registers; zero elsewhere.
    memory: dict[int, int] = {}
    registers: dict[str, int] = {"PC": 0x34180D3C}

    def read_memory(self, address: int, length_bytes: int) -> dict[int, int]:
        n_words = max(1, length_bytes // 4)
        return {address + i * 4: self.memory.get(address + i * 4, 0) for i in range(n_words)}

    def core_registers(self) -> dict[str, int]:
        return dict(self.registers)

    def read_ranges(self, ranges: list[tuple[int, int]]) -> dict[int, int]:
        words: dict[int, int] = {}
        for address, length_bytes in ranges:
            words.update(self.read_memory(address, length_bytes))
        return words

    def identify_and_read(self, address: int, length_bytes: int):
        return None  # the bench always takes the identify-then-read path

    # What a readback returns: None for exactly what was written, bytes for
    # something else, a string to raise with. Class-level so a test can set it.
    readback: bytes | str | None = None

    def program_flash(self, image: str, address: int, loader: object) -> str:
        self._written = Path(image).read_bytes()
        return "File download complete\n"

    def read_back(self, address: int, size: int, loader: object) -> bytes:
        if isinstance(self.readback, str):
            raise probe_mod.ProbeError(self.readback)
        return (self._written if self.readback is None else self.readback)[:size]

    # Reset behaviour is class-level so a test can set the halted PC to each of
    # the three shapes real hardware produces: landed on the vector, landed
    # somewhere else, or no answer at all because the reset closed the port.
    reset_pc: int | None = 0x0807FA98

    def reset_and_halt(self, access_port: int) -> tuple[int | None, str]:
        return self.reset_pc, "Core halted\n"

    def release_core(self, access_port: int) -> str:
        return "Core run\n"

    # What an SWO capture returns, or the error it raises. Class-level so a
    # test can set it.
    swo_text: str = ""
    swo_raises: str | None = None

    def capture_swo(self, clock_hz: int, seconds: float, itm_port: int) -> str:
        if self.swo_raises:
            raise probe_mod.ProbeError(self.swo_raises)
        return self.swo_text


@pytest.fixture
def bench(monkeypatch, tmp_path):
    """A whole fake bench: no CLI, no probe, no serial port, no board."""
    # A real (minimal) ELF32 whose one loadable segment covers this family's RAM
    # vector table, because run_firmware now refuses anything else before it
    # starts a server (red-team RT1, 2026-09-03).
    from elfgen import minimal_elf32
    vt = probe_mod.TargetInfo(connected=True, device_id=_FakeProbe.device_id).quirks.vector_table_addr
    elf = tmp_path / "image.elf"
    elf.write_bytes(minimal_elf32(vt - 0x400, 0x1000))
    # flash_firmware takes raw binaries only, and checks the file before it
    # touches the probe, so reaching its body needs a real one.
    raw = tmp_path / "image.bin"
    raw.write_bytes(b"\x00" * 16)

    state = SimpleNamespace(
        elf=elf,
        bin=raw,
        markers=(0x34200000, 0x34180841, 2048),
        gdb_output="",
        console_text="",
        console_buffered="",
        console_port="/dev/fake0",
        console_raises=None,
        shares=True,
    )

    class _FakeCapture:
        def __init__(self, port: str, baud: int) -> None:
            self._buffer = ""

        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> None:
            pass

        def collect(self, seconds: float) -> str:
            if state.console_raises:
                # Whatever arrived before the device went away stays readable,
                # which is what pyserial leaves behind too.
                self._buffer = state.console_text
                raise OSError(state.console_raises)
            return state.console_text

        @property
        def text(self) -> str:
            return self._buffer

        @property
        def buffered(self) -> str:
            return state.console_buffered

    def _attached(tc: object) -> list[probe_mod.ProbeInfo]:
        # As parse_probe_list builds it: a mapped console is labelled as such.
        return [probe_mod.ProbeInfo(
            serial="AAA1", console_port=state.console_port,
            console_source="probe list" if state.console_port else None,
        )]

    monkeypatch.setattr(server, "_toolchain", lambda: SimpleNamespace(gdb=Path("arm-none-eabi-gdb")))
    monkeypatch.setattr(server, "_list_probes", _attached)

    class _Session:
        """GDB against the bench's held server: connects and leaves."""

        def __init__(self, gdb, elf, port) -> None:
            pass

        def close(self, detach: bool = True) -> None:
            pass

    monkeypatch.setattr(server.gdbmi, "Session", _Session)
    # objdump has a test of its own; here no instruction is known.
    monkeypatch.setattr(server, "_instruction", lambda toolchain, elf, address: None)
    monkeypatch.setattr(
        server, "list_probes_with_unreadable", lambda tc: (_attached(tc), 0, [])
    )
    monkeypatch.setattr(server, "Probe", _FakeProbe)
    # The GDB servers this bench holds, by serial. They share the probe, as one
    # started with -t does where stlink-server is installed, unless a test sets
    # state.shares to False.
    held: dict = {}

    def acquire(*args, serial=None, **kwargs):
        held[serial] = SimpleNamespace(shared=state.shares, interface=kwargs.get("interface", "swd"),
                                       attach=kwargs.get("attach", False), recent_output=lambda n: [],
                                       port=61234)
        return held[serial]

    def release(serial=None) -> bool:
        if serial is None:
            found = bool(held)
            held.clear()
            return found
        return held.pop(serial, None) is not None

    monkeypatch.setattr(server, "acquire_server", acquire)
    monkeypatch.setattr(server, "held_server", lambda serial: held.get(serial))
    monkeypatch.setattr(server, "server_is_held",
                        lambda serial=None: bool(held) if serial is None else serial in held)
    monkeypatch.setattr(server, "held_serials", lambda: list(held))
    monkeypatch.setattr(server, "release_server", release)
    state.held = held
    monkeypatch.setattr(
        server, "load_and_run", lambda *a, **k: (*state.markers, state.gdb_output)
    )
    monkeypatch.setattr(server.console_mod, "ConsoleCapture", _FakeCapture)
    # As on a machine without udev's port names, so a test takes the same path
    # here as in CI; the udev path has tests of its own. Without them the
    # console is looked up in pyserial's port list, which is the machine's own,
    # so it is emptied here.
    monkeypatch.setattr(server, "_BY_ID_DIR", tmp_path / "no-by-id")
    from serial.tools import list_ports
    monkeypatch.setattr(list_ports, "comports", lambda *a, **k: [])

    server._held.clear()
    server._held_by.clear()
    server._gdb_ports.clear()
    server._SETTLED_PORTS.clear()
    yield state
    server._held.clear()
    server._held_by.clear()
    server._gdb_ports.clear()


class TestEveryToolKeepsTheContract:
    @pytest.mark.parametrize("name", sorted(TOOL_CALLS))
    def test_on_a_working_bench(self, name: str, bench) -> None:
        files = {
            "run_firmware": {"elf_path": str(bench.elf)},
            "flash_firmware": {"image_path": str(bench.bin)},
        }
        out = call(name, **files.get(name, {}))
        assert_contract(out, name)
        if name == "flash_firmware":
            assert out["status"] == result.VERIFIED, "the bench must reach the write"

    @pytest.mark.parametrize("name", sorted(TOOL_CALLS))
    def test_with_no_toolchain_installed(self, name: str, monkeypatch) -> None:
        """The most common real failure, and one every tool must survive."""
        def boom() -> None:
            raise ToolchainError("STM32CubeCLT is not installed")

        monkeypatch.setattr(server, "_toolchain", boom)
        server._held.clear()
        assert_contract(call(name), name)

    @pytest.mark.parametrize("name", sorted(TOOL_CALLS))
    def test_with_no_probes_attached(self, name: str, monkeypatch) -> None:
        monkeypatch.setattr(server, "_toolchain", lambda: object())
        monkeypatch.setattr(server, "_list_probes", lambda tc: [])
        monkeypatch.setattr(server, "list_probes_with_unreadable", lambda tc: ([], 0, []))
        server._held.clear()
        assert_contract(call(name), name)


class TestRunFirmwareCannotReportASuccessItDidNotSee:
    """The blocking defect, pinned.

    GDB in -batch aborts its script on the first failed command, so a failed
    load leaves both end markers absent. Measured, not assumed: a failing
    `monitor` in a -x script printed nothing after it and exited 1.
    """

    def test_a_failed_load_is_not_success_even_with_a_busy_console(self, bench) -> None:
        """The shape that made this dangerous.

        The previous image is still resident and still printing, which is the
        steady state of any firmware that blinks. So the console is busy while
        nothing at all was loaded, and the old code called that a verified run.
        """
        bench.markers = (None, None, None)
        bench.console_text = "TX seq=41 ts=0100AF3C22\nTX seq=42 ts=0100B04711\n"

        r = call("run_firmware", elf_path=str(bench.elf))

        assert_contract(r)
        assert r["status"] == result.FAILED
        assert "previous" in r["error"].lower()

    def test_a_failed_load_keeps_what_gdb_and_the_server_said(
        self, bench, monkeypatch
    ) -> None:
        """GDB's output was discarded on exactly the path where it holds the
        cause, which the README listed as a known defect."""
        bench.markers = (None, None, None)
        bench.gdb_output = "Loading section .isr_vector\nError finishing flash operation\n"
        monkeypatch.setattr(
            server, "acquire_server",
            lambda *a, **k: SimpleNamespace(recent_output=lambda n: ["Target halted"]),
        )
        r = call("run_firmware", elf_path=str(bench.elf))
        assert_contract(r)
        assert r["status"] == result.FAILED
        assert "Error finishing flash operation" in r["gdb_output"]
        assert r["gdb_server_output"] == ["Target halted"]

    def test_a_failure_after_the_server_started_says_the_probe_is_held(
        self, bench, monkeypatch
    ) -> None:
        """N3. The server outlives a failed load by design, and the next
        read_memory then refused with "held" for a reason nobody had said."""
        from stlink_mcp.runner import RunError

        def gdb_times_out(*args, **kwargs):
            raise RunError("arm-none-eabi-gdb did not finish within 180s")

        monkeypatch.setattr(server, "load_and_run", gdb_times_out)
        r = call("run_firmware", elf_path=str(bench.elf))
        assert_contract(r)
        assert r["status"] == result.FAILED
        assert r["gdb_server_held"] is True and "gdb_server_note" not in r, "it shares the probe"
        bench.shares = False
        r = call("run_firmware", elf_path=str(bench.elf))
        assert "read_memory" in r["gdb_server_note"] and "release_probe" in r["gdb_server_note"]

    def test_a_run_says_it_reset_the_target(self, bench) -> None:
        """N3. The GDB batch starts with `monitor reset`, and the docstring said
        so, but the result did not (found 2026-10-03)."""
        bench.console_text = "tick 0\n"
        loud = call("run_firmware", elf_path=str(bench.elf))
        bench.console_text = ""
        silent = call("run_firmware", elf_path=str(bench.elf))
        assert (loud["status"], silent["status"]) == (result.VERIFIED, result.UNVERIFIED)
        assert loud["target_was_reset"] is True and silent["target_was_reset"] is True

    def test_a_batch_that_stopped_early_does_not_say_whether_it_reset(self, bench) -> None:
        bench.markers = (None, None, None)
        r = call("run_firmware", elf_path=str(bench.elf))
        assert r["status"] == result.FAILED
        assert r["target_was_reset"] is None and "not known" in r["reset_note"]

    def test_a_silent_run_does_not_send_the_agent_to_release_the_probe(self, bench) -> None:
        """core_registers reaches the core through the server the run keeps (H7
        and N6, 2026-10-03). Releasing first, as this used to say, can close an
        STM32N6's debug port until a power cycle."""
        r = call("run_firmware", elf_path=str(bench.elf))
        assert r["status"] == result.UNVERIFIED
        assert "core_registers" in r["unverified_because"] and "release_probe" not in r["unverified_because"]

    def test_running_out_of_gdb_ports_is_a_result_not_an_exception(
        self, bench, monkeypatch
    ) -> None:
        from stlink_mcp.runner import RunError

        def no_ports(serial):
            raise RunError("No free GDB port pair")

        monkeypatch.setattr(server, "_gdb_port_for", no_ports)
        r = call("run_firmware", elf_path=str(bench.elf))
        assert_contract(r)
        assert "No free GDB port pair" in r["error"]

    def test_a_silent_target_is_unverified_rather_than_a_run(self, bench) -> None:
        bench.console_text = ""
        r = call("run_firmware", elf_path=str(bench.elf))
        assert_contract(r)
        assert r["status"] == result.UNVERIFIED

    def test_no_console_is_unverified_rather_than_assumed_good(self, bench) -> None:
        """This one used to be ok=True with verified_by "nothing"."""
        bench.console_port = None
        r = call("run_firmware", elf_path=str(bench.elf))
        assert_contract(r)
        assert r["status"] == result.UNVERIFIED
        assert "console" in r["unverified_because"].lower()

    def test_a_flash_linked_image_is_refused_before_any_load(self, bench) -> None:
        """RT1 (2026-09-03): a normally linked build (0x08000000) handed to
        run_firmware made GDB program flash and then start the core from garbage,
        while the tool reported unverified. The refusal has to come before a
        server is started, so nothing is written."""
        from elfgen import minimal_elf32
        bench.elf.write_bytes(minimal_elf32(0x08000000, 0x1000))
        r = call("run_firmware", elf_path=str(bench.elf))
        assert_contract(r)
        assert r["status"] == result.FAILED
        assert "not linked" in r["error"] and "0x08000000" in r["error"]
        assert "vector_table_addr" in r

    def test_a_flash_segment_beside_a_ram_vector_table_is_refused_before_any_load(
        self, bench, monkeypatch
    ) -> None:
        """The same failure for an image the first check let through: its
        vector table is in RAM, and another segment is linked for flash."""
        from elfgen import elf32_segments

        def must_not_start(*args, **kwargs):
            raise AssertionError("a GDB server was started for a refused image")

        vt = probe_mod.TargetInfo(connected=True, device_id=_FakeProbe.device_id).quirks.vector_table_addr
        bench.elf.write_bytes(elf32_segments((vt, 0x400), (0x70000000, 0x800)))
        monkeypatch.setattr(server, "acquire_server", must_not_start)
        r = call("run_firmware", elf_path=str(bench.elf))
        assert_contract(r)
        assert r["status"] == result.FAILED
        assert "0x70000000" in r["error"]

    def test_a_file_that_is_not_an_elf_is_refused(self, bench) -> None:
        bench.elf.write_bytes(b"\x7fELF not really")
        r = call("run_firmware", elf_path=str(bench.elf))
        assert_contract(r)
        assert r["status"] == result.FAILED
        assert "ELF" in r["error"]

    def test_console_output_is_what_verifies_a_run(self, bench) -> None:
        bench.console_text = "target link test: tx\n  probe ok\n"
        r = call("run_firmware", elf_path=str(bench.elf))
        assert_contract(r)
        assert r["status"] == result.VERIFIED
        assert "console" in r["verified_by"]

    def test_the_probes_backlog_never_verifies_a_run(self, bench) -> None:
        """Measured 2026-10-01: an image that printed nothing was verified on the
        previous image's lines, which the probe had kept while the port was
        closed. They are reported apart and are not evidence."""
        bench.console_buffered = "previous image tick 41\r\n"
        bench.console_text = ""
        r = call("run_firmware", elf_path=str(bench.elf))
        assert_contract(r)
        assert r["status"] == result.UNVERIFIED
        assert r["console"] == ""
        assert r["console_buffered"] == "previous image tick 41\r\n"
        assert r["console_buffered_bytes"] == 24
        assert "not counted as evidence" in r["console_buffered_note"]

    def test_a_run_is_verified_by_its_own_output_beside_a_backlog(self, bench) -> None:
        bench.console_buffered = "previous image tick 41\r\n"
        bench.console_text = "new image tick 0\r\n"
        r = call("run_firmware", elf_path=str(bench.elf))
        assert_contract(r)
        assert r["status"] == result.VERIFIED
        assert r["console"] == "new image tick 0\r\n"
        assert "console_buffered" in r

    def test_no_backlog_adds_no_backlog_fields(self, bench) -> None:
        bench.console_text = "tick 0\n"
        r = call("run_firmware", elf_path=str(bench.elf))
        assert not any(k.startswith("console_buffered") for k in r)

    def test_a_console_that_drops_mid_capture_keeps_what_it_already_had(
        self, bench
    ) -> None:
        """A USB replug raises SerialException, an OSError, which the outer
        clause does not catch. Letting it out discarded the completed load, the
        markers, the partial buffer, and the fact that the server is now held."""
        bench.console_text = "target link test: tx\n"
        bench.console_raises = "device reports readiness to read but returned no data"

        r = call("run_firmware", elf_path=str(bench.elf))

        assert_contract(r)
        assert r["console"] == "target link test: tx\n"
        assert r["image_initial_sp"] == "0x34200000"
        assert "readiness to read" in r["console_error"]

    def test_a_console_that_drops_before_saying_anything_is_unverified(
        self, bench
    ) -> None:
        bench.console_text = ""
        bench.console_raises = "device disconnected"

        r = call("run_firmware", elf_path=str(bench.elf))

        assert_contract(r)
        assert r["status"] == result.UNVERIFIED
        assert "device disconnected" in r["unverified_because"]

    def test_the_reported_addresses_are_not_claimed_as_observations(self, bench) -> None:
        """image_initial_sp and image_entry_point come from the ELF's vector
        table and are written INTO the core. They were once called
        stack_pointer and program_counter, which read as a reading of where the
        core is. The names carry it; the note that also said so went with the
        budget for result text (2026-10-03)."""
        bench.console_text = "up\n"
        r = call("run_firmware", elf_path=str(bench.elf))
        assert "stack_pointer" not in r and "program_counter" not in r
        assert r["image_initial_sp"] == "0x34200000" and "image_note" not in r


class TestRunFirmwareKeepsTheProbe:
    """The GDB server keeps the probe after a run, so a program that closes the
    debug port cannot lock the debugger out. From b6e5aa8 it let go after every
    run, which turned such a program into a reset or a power cycle; whether to
    let go is the agent's choice, with release_probe (2026-10-04)."""

    @pytest.fixture
    def ran(self, bench, monkeypatch):
        import dataclasses
        part = dataclasses.replace(probe_mod.for_device_id(_FakeProbe.device_id), debug_closes_on_run=False)
        monkeypatch.setattr(probe_mod, "for_device_id", lambda did: part)
        made: list = []
        original = _FakeProbe.__init__

        def remember(self, *args, **kwargs):
            original(self, *args, **kwargs)
            made.append(kwargs.get("shared", False))

        monkeypatch.setattr(_FakeProbe, "__init__", remember)
        bench.console_text = "up\n"
        out = call("run_firmware", elf_path=str(bench.elf))
        assert out["status"] == result.VERIFIED and out["gdb_server_held"] is True
        made.clear()
        return made

    def test_on_every_part(self, bench, ran) -> None:
        assert list(bench.held) == ["AAA1"]

    @pytest.mark.parametrize("name", ["read_memory", "read_fault_status", "locate_fault", "capture_swo"])
    def test_the_reading_tools_connect_beside_it(self, bench, ran, name) -> None:
        out = call(name)
        assert_contract(out, name)
        assert out["status"] != result.FAILED or "holding" not in out["error"], out
        assert ran and all(ran), "every programmer connect is made in shared mode"
        assert list(bench.held) == ["AAA1"], "and the server is still held"

    def test_board_state_reads_live_beside_it(self, bench, ran) -> None:
        out = call("board_state")
        assert ran == [True] and out["status"] == result.VERIFIED, out

    @pytest.mark.parametrize("name, args", [("reset_target", {}), ("flash_firmware", {"image_path": None})])
    def test_a_reset_or_a_flash_write_stops_it_first_and_says_so(self, bench, ran, name, args) -> None:
        """Either, made beside the server, broke its GDB session (2026-10-03)."""
        args = {k: str(bench.bin) for k in args}
        out = call(name, **args)
        assert_contract(out, name)
        assert out["released_probe_first"] is True and bench.held == {}
        assert not any(ran), "nothing is shared once the server is gone"

    def test_keep_probe_false_lets_go_once_the_program_starts(self, bench, monkeypatch) -> None:
        """A held server resets and halts a core that locks up, which clears its
        fault registers (H7, F407 and N6, 2026-10-04); letting go keeps them."""
        bench.console_text = ""
        made: list = []
        original = _FakeProbe.__init__

        def remember(self, *args, **kwargs):
            original(self, *args, **kwargs)
            made.append(kwargs.get("shared", False))

        monkeypatch.setattr(_FakeProbe, "__init__", remember)
        monkeypatch.setattr(_FakeProbe, "core", probe_mod.CoreState(0x01010000, 0x01000000, (0x34180500,) * 2))
        out = call("run_firmware", elf_path=str(bench.elf), keep_probe=False)
        assert_contract(out)
        assert out["gdb_server_held"] is False and bench.held == {}
        assert out["status"] == result.VERIFIED and made[-1] is False, "sampled with nothing held"
        assert "power cycle" in out["warning"], "the bench is an STM32N6"

    def test_a_kept_probe_carries_no_release_warning(self, bench) -> None:
        bench.console_text = "up\n"
        assert "warning" not in call("run_firmware", elf_path=str(bench.elf))

    def test_release_resumes_a_stopped_core_through_gdb_before_stopping_the_server(self, bench, monkeypatch) -> None:
        """Stopping a server in shared mode left the F407's core halted two times
        in five (2026-10-04); a GDB detach resumes it."""
        order: list = []

        class _Session:
            def __init__(self, gdb, elf, port) -> None:
                order.append(("attach", port))

            def close(self, detach: bool = True) -> None:
                order.append(("detach", detach))

        monkeypatch.setattr(server.gdbmi, "Session", _Session)
        monkeypatch.setattr(server, "_core_state_after_release", lambda serial, stop: "running again")
        original = server.release_server
        monkeypatch.setattr(server, "release_server", lambda serial=None: order.append("stop") or original(serial))
        bench.console_text = "up\n"
        call("run_firmware", elf_path=str(bench.elf))
        server._stopped["AAA1"] = {"address": "0x34180500", "function": "writer"}
        out = call("release_probe", probe_serial="AAA1")
        assert order == [("attach", 61234), ("detach", True), "stop"]
        assert out["core_after"] == {"AAA1": "running again"}

    def test_a_second_run_stops_the_previous_image_before_the_console_opens(self, bench, monkeypatch) -> None:
        """Through a held server the previous image ran on until the load's reset,
        and a line it printed after the port opened verified an image that
        prints nothing (H7, 2026-10-04). A new server halts it as it starts."""
        order: list = []

        class _Session:
            def __init__(self, gdb, elf, port) -> None:
                order.append("attach")

            def close(self, detach: bool = True) -> None:
                order.append(f"close, detach {detach}")

        monkeypatch.setattr(server.gdbmi, "Session", _Session)
        capture = server.console_mod.ConsoleCapture
        monkeypatch.setattr(server.console_mod, "ConsoleCapture",
                            lambda *a, **k: order.append("console opens") or capture(*a, **k))
        bench.console_text = "up\n"
        call("run_firmware", elf_path=str(bench.elf))
        call("run_firmware", elf_path=str(bench.elf))
        assert order == ["console opens", "attach", "close, detach False", "console opens"]

    def test_nothing_is_said_when_nothing_was_held(self, bench) -> None:
        assert "released_probe_first" not in call("reset_target")

    def test_without_stlink_server_the_reading_tools_refuse(self, bench, monkeypatch) -> None:
        bench.shares = False
        bench.console_text = "up\n"
        call("run_firmware", elf_path=str(bench.elf))
        out = call("read_memory")
        assert out["status"] == result.FAILED and "stlink-server" in out["error"]


class TestALockupUnderAHeldServerIsNamed:
    """While a GDB server held the probe, a core that faulted inside its fault
    handler was reset and halted at its reset vector, DFSR reading VCATCH and
    the fault registers clear, on the H7, the F407 and the N6 (2026-10-04)."""

    @pytest.fixture
    def caught(self, bench, monkeypatch):
        monkeypatch.setattr(_FakeProbe, "core", probe_mod.CoreState(0x00030003, 0, (0,) * 5, 0x9))
        monkeypatch.setattr(_FakeProbe, "memory", {faults.DFSR: 0x9})
        bench.console_text = "up\n"

    def test_board_state_beside_the_server_says_what_it_likely_is(self, bench, caught) -> None:
        call("run_firmware", elf_path=str(bench.elf))
        core = call("board_state")["core"]
        assert core["state"] == "halted" and core["dfsr"] == "0x00000009"
        assert "keep_probe=False" in core["state_note"]

    def test_read_fault_status_beside_the_server_says_it(self, bench, caught) -> None:
        call("run_firmware", elf_path=str(bench.elf))
        out = call("read_fault_status")
        assert out["any_fault_active"] is False and "keep_probe=False" in out["vector_catch_note"]
        assert out["status"] == result.UNVERIFIED, "a clean read there is not evidence of no fault"
        assert "keep_probe=False" in out["unverified_because"]

    def test_with_nothing_held_neither_says_it(self, bench, caught) -> None:
        core = call("board_state")["core"]
        assert "state_note" not in core and "dfsr" not in core, "DFSR only as the note's evidence"
        out = call("read_fault_status")
        assert "vector_catch_note" not in out and out["status"] == result.VERIFIED


class TestFaultAddressRegistersOnlyWhenValid:
    """MMFAR and BFAR hold a stale or reset value unless CFSR marks them valid;
    three of four agents in an evaluation named one as easy to take for the
    faulting address."""

    def test_an_invalid_one_is_left_out(self, bench, monkeypatch) -> None:
        monkeypatch.setattr(_FakeProbe, "memory", {faults.CFSR: 0x00010000, faults.MMFAR: 0x3597024A,
                                                   faults.BFAR: 0x3597024A})
        raw = call("read_fault_status")["raw_registers"]
        assert "MMFAR" not in raw and "BFAR" not in raw and raw["CFSR"] == "0x00010000"

    def test_a_valid_one_is_given_twice(self, bench, monkeypatch) -> None:
        monkeypatch.setattr(_FakeProbe, "memory", {faults.CFSR: (1 << 9) | (1 << 15), faults.BFAR: 0x40001234})
        out = call("read_fault_status")
        assert out["raw_registers"]["BFAR"] == "0x40001234" and out["bus_fault_address"] == "0x40001234"
        assert "MMFAR" not in out["raw_registers"]


class TestWhereTheCoreRunsSaysWhetherASilentImageStarted:
    """Program-counter samples read without halting the core lay inside the
    running image on the H7, the F407 and the N6 (2026-10-03). The bench ELF's
    one segment is 0x34180000 to 0x34181000."""

    @pytest.fixture
    def open_part(self, bench):
        bench.console_text = ""

    @pytest.mark.parametrize("dhcsr, samples, status, says", [
        (0x01010000, (0x34180500, 0x34180504), result.VERIFIED, "executing this image's code: 2 of 2"),
        (0x01010000, (0x34180500, 0x08000100), result.UNVERIFIED, "Only 1 of 2"),
        (0x00190001, (0x341804F8,) * 2, result.UNVERIFIED, "locked up"),
    ])
    def test_a_silent_run(self, bench, open_part, monkeypatch, dhcsr, samples, status, says) -> None:
        monkeypatch.setattr(_FakeProbe, "core", probe_mod.CoreState(dhcsr, 0x01000000, samples))
        out = call("run_firmware", elf_path=str(bench.elf))
        assert_contract(out)
        reason = out.get("verified_by") or out.get("unverified_because")
        assert out["status"] == status and says in reason and out["core"]["pc_samples"]

    def test_not_beside_a_server_that_keeps_the_probe_to_itself(self, bench, monkeypatch) -> None:
        """Without stlink-server the programmer cannot read beside the server."""
        bench.shares = False
        monkeypatch.setattr(_FakeProbe, "core", probe_mod.CoreState(0x01010000, 0x01000000, (0x34180500,)))
        out = call("run_firmware", elf_path=str(bench.elf))
        assert out["status"] == result.UNVERIFIED and "core" not in out

    def test_board_state_says_where_with_the_elf(self, bench, monkeypatch) -> None:
        monkeypatch.setattr(_FakeProbe, "core", probe_mod.CoreState(0x01010000, 0x01000000, (0x34180500, 0x34180500, 0x34180504)))
        monkeypatch.setattr(server, "_symbol", lambda tc, elf, addr: {"function": "loop", "file": "a.c", "line": 3,
                                                                      "address": hex(addr)})
        out = call("board_state", elf_path=str(bench.elf))
        assert out["core"]["state"] == "running" and len(out["core"]["pc_samples"]) == 3
        assert [p["address"] for p in out["core"]["pc_places"]] == ["0x34180500", "0x34180504"]


class TestTheConsoleSettingMeansOneThingEverywhere:
    """STLINK_MCP_CONSOLE was consulted by capture_console only when the probe
    list mapped no console, and never by run_firmware, so a board wired to a
    separate USB serial adapter could not get a verified run. The README said it
    was for when the guess is wrong, and it could not override a guess at all."""

    CONSOLE = "STLINK_MCP_CONSOLE"

    def test_run_firmware_verifies_on_the_port_the_setting_names(
        self, bench, monkeypatch
    ) -> None:
        bench.console_port = None
        bench.console_text = "hello from a board with a USB serial adapter\n"
        monkeypatch.setenv(self.CONSOLE, "AAA1=/dev/ttyUSB7")
        out = call("run_firmware", elf_path=str(bench.elf))
        assert_contract(out)
        assert out["status"] == result.VERIFIED
        assert out["console_port"] == "/dev/ttyUSB7"
        assert out["console_port_source"] == self.CONSOLE
        assert self.CONSOLE in out["verified_by"]

    def test_the_setting_overrides_the_probe_list(self, bench, monkeypatch) -> None:
        bench.console_text = "up\n"
        monkeypatch.setenv(self.CONSOLE, "AAA1=/dev/ttyUSB7")
        assert call("run_firmware", elf_path=str(bench.elf))["console_port"] == "/dev/ttyUSB7"

    def test_the_probe_lists_own_mapping_is_named_as_such(self, bench, monkeypatch) -> None:
        bench.console_text = "up\n"
        monkeypatch.delenv(self.CONSOLE, raising=False)
        out = call("run_firmware", elf_path=str(bench.elf))
        assert out["console_port_source"] == "probe list"
        assert "own port" in out["verified_by"]

    def test_a_bare_path_is_not_applied_across_two_probes(self, bench, monkeypatch) -> None:
        bench.console_port = None
        monkeypatch.setattr(server, "_list_probes", lambda tc: [
            probe_mod.ProbeInfo(serial="AAA1"), probe_mod.ProbeInfo(serial="BBB2"),
        ])
        monkeypatch.setenv(self.CONSOLE, "/dev/ttyUSB7")
        out = call("capture_console", probe_serial="AAA1")
        assert_contract(out)
        assert out["status"] == result.FAILED
        assert "applied to none" in out["console_note"]

    def test_list_probes_shows_the_console_the_tools_will_use(
        self, bench, monkeypatch
    ) -> None:
        monkeypatch.setenv(self.CONSOLE, "AAA1=/dev/ttyUSB7")
        probe = call("list_probes")["probes"][0]
        assert probe["console_port"] == "/dev/ttyUSB7"
        assert probe["console_port_source"] == self.CONSOLE

    def test_capture_console_keeps_the_probes_backlog_out_of_its_output(self, bench) -> None:
        bench.console_buffered = "x" * 1500
        bench.console_text = "live\n"
        out = call("capture_console")
        assert_contract(out)
        assert out["console"] == "live\n"
        assert out["console_buffered_bytes"] == 1500
        assert len(out["console_buffered"]) == 1000
        assert "so it may be older" in out["console_buffered_note"]
        assert "the part in the middle is missing" in out["console_buffered_note"]
        assert "last 1000 characters" in out["console_buffered_note"]

    def test_capture_console_keeps_what_arrived_before_an_unplug(self, bench) -> None:
        """A SerialException mid-capture used to escape the tool with the text
        already read thrown away."""
        bench.console_text = "boot ok\n"
        bench.console_raises = "device disconnected"
        out = call("capture_console")
        assert_contract(out)
        assert out["status"] == result.VERIFIED
        assert "partial" in out["verified_by"]
        assert out["console"] == "boot ok\n"
        assert "device disconnected" in out["console_error"]

    def test_a_capture_that_heard_nothing_is_unverified(self, bench) -> None:
        """It came back verified with empty output from an STM32F407 whose
        UART was sending, measured from its registers, to an adapter not yet
        wired to the pin (2026-10-01). Read as a verdict, that says the target
        is silent, which the port cannot tell from a missing wire."""
        bench.console_buffered = "old\n"
        bench.console_text = ""
        out = call("capture_console", seconds=2)
        assert_contract(out)
        assert out["status"] == result.UNVERIFIED
        assert "wired" in out["unverified_because"]
        assert "115200" in out["unverified_because"]
        assert out["console"] == ""
        assert out["console_buffered"] == "old\n"


class TestSwoIsCapturedAndItsSideEffectSaid:
    """capture_swo, measured on an STM32F407 and an STM32H7 on 2026-10-02. The
    programmer's capture turns on ITM and hands the SWO pin to the trace unit,
    and on the F407 the pin assignment outlived reset_target; a clock 6% off
    garbled the text and one twice the real clock gave nothing."""

    def test_text_that_arrived_is_verified_and_the_trace_left_on_is_said(self, bench, monkeypatch) -> None:
        monkeypatch.setattr(_FakeProbe, "swo_text", "f407 itm tick 2\r\n")
        out = call("capture_swo")
        assert_contract(out)
        assert out["status"] == result.VERIFIED and out["swo"] == "f407 itm tick 2\r\n"
        assert out["trace_left_enabled"] is True and "power cycle" in out["trace_note"]
        assert out["target_was_reset"] is False and (out["core_clock_hz"], out["itm_port"]) == (16_000_000, 0)

    def test_nothing_arrived_is_unverified_and_names_the_clock(self, bench) -> None:
        out = call("capture_swo")
        assert_contract(out)
        assert out["status"] == result.UNVERIFIED
        why = out["unverified_because"]
        assert "16000000" in why and "ITM port 0" in why and "wired" in why
        assert out["trace_left_enabled"] is True

    def test_a_capture_at_a_clock_6pc_low_says_the_clock_may_be_wrong(self, bench, monkeypatch) -> None:
        with open(Path(__file__).parent / "fixtures" / "swo_f407_itm_clock_6pc_low.log",
                  encoding="utf-8", errors="replace", newline="") as handle:
            monkeypatch.setattr(_FakeProbe, "swo_text", probe_mod.parse_swv_log(handle.read()))
        out = call("capture_swo")
        assert out["status"] == result.VERIFIED
        assert "core clock" in out["swo_garbled_note"]

    def test_a_tenth_unreadable_is_enough_for_the_note(self, bench, monkeypatch) -> None:
        line = "stlink-mcp tick 38\r\n"
        monkeypatch.setattr(_FakeProbe, "swo_text", line * 4 + "\ufffd" * 9)
        assert "swo_garbled_note" in call("capture_swo")
        monkeypatch.setattr(_FakeProbe, "swo_text", line * 4 + "\ufffd" * 8)
        assert "swo_garbled_note" not in call("capture_swo")

    def test_a_failed_capture_is_failed(self, bench, monkeypatch) -> None:
        monkeypatch.setattr(_FakeProbe, "swo_raises", "The programmer did not start SWO reception.")
        out = call("capture_swo")
        assert_contract(out)
        assert out["status"] == result.FAILED and "did not start" in out["error"]

    @pytest.mark.parametrize("args", [{"itm_port": 32}, {"itm_port": -1}, {"core_clock_hz": 0},
                                      {"seconds": 0}, {"seconds": 61}])
    def test_arguments_out_of_range_are_refused(self, bench, args) -> None:
        out = call("capture_swo", **args)
        assert_contract(out)
        assert out["status"] == result.FAILED

    def test_a_held_probe_is_refused(self, bench, monkeypatch) -> None:
        monkeypatch.setattr(server, "server_is_held", lambda serial=None: True)
        out = call("capture_swo")
        assert out["status"] == result.FAILED and "holding" in out["error"].lower()


class TestReadingByName:
    """read_memory's symbol and register paths. The variable side uses the ELF
    fixtures and the bytes read from a NUCLEO-H743ZI2 on 2026-10-02; the register
    side needs the SVD files of an STM32CubeCLT install, and skips without one."""

    ELF = str(Path(__file__).parent / "fixtures" / "elf" / "h7_vars_dwarf5.elf")

    def _lay_out(self, monkeypatch, *, image_matches: bool) -> None:
        from stlink_mcp import symbols
        from test_symbols import DUMP
        memory: dict[int, int] = {}
        base, code = symbols.first_code(self.ELF, 64)
        if image_matches:
            for i in range(0, 64, 4):
                memory[base + i] = int.from_bytes(code[i:i + 4], "little")
        for i in range(0, len(DUMP), 4):
            memory[0x24000130 + i] = int.from_bytes(DUMP[i:i + 4], "little")
        monkeypatch.setattr(_FakeProbe, "memory", memory)

    def test_a_variable_on_a_target_running_its_elf_is_verified(self, bench, monkeypatch) -> None:
        from test_symbols import EXPECTED
        self._lay_out(monkeypatch, image_matches=True)
        out = call("read_memory", address=None, symbol="g_state", elf_path=self.ELF)
        assert_contract(out)
        assert out["status"] == result.VERIFIED, out
        assert out["value"] == EXPECTED and out["type"] == "state_t"
        assert out["address"] == "0x24000130" and "match" in out["verified_by"]

    def test_a_target_running_something_else_is_not_verified(self, bench, monkeypatch) -> None:
        """The address and the type come from the ELF; on a target running
        another build they may describe some other variable's bytes."""
        self._lay_out(monkeypatch, image_matches=False)
        out = call("read_memory", address=None, symbol="g_state", elf_path=self.ELF)
        assert_contract(out)
        assert out["status"] == result.UNVERIFIED
        assert "not running this ELF" in out["unverified_because"]
        assert "value" not in out and "decoded_with_this_elf" in out

    def test_nothing_at_the_address_points_at_the_elf(self, bench, monkeypatch) -> None:
        def nothing(self, ranges):
            raise probe_mod.ProbeError("No data read at 0x24000130.")
        monkeypatch.setattr(_FakeProbe, "read_ranges", nothing)
        out = call("read_memory", address=None, symbol="g_state", elf_path=self.ELF)
        assert out["status"] == result.FAILED and "another part" in out["error"]

    def test_a_name_that_is_not_one_variable_fails_before_the_probe(self, bench) -> None:
        out = call("read_memory", address=None, symbol="s_count", elf_path=self.ELF)
        assert out["status"] == result.FAILED and "names 2 variables" in out["error"]

    @pytest.mark.parametrize("args, says", [
        ({"address": None, "symbol": "g_state"}, "needs elf_path"),
        ({"symbol": "g_state", "elf_path": ELF}, "this call gave address and symbol"),
        ({"address": None}, "exactly one of address, symbol or register."),
    ])
    def test_the_arguments_must_name_one_thing(self, bench, args, says) -> None:
        out = call("read_memory", **args)
        assert_contract(out)
        assert out["status"] == result.FAILED and says in out["error"]

    def _svd(self, monkeypatch):
        from stlink_mcp import svd
        from stlink_mcp.toolchain import ToolchainError, find_toolchain
        try:
            directory = svd.svd_dir(find_toolchain().root)
        except ToolchainError:
            pytest.skip("no STM32CubeCLT install")
        if not (directory / "STM32H743.svd").is_file():
            pytest.skip("no SVD files")
        monkeypatch.setattr(server, "_toolchain", lambda: SimpleNamespace(root=directory.parent))

    def test_a_register_is_decoded_and_its_read_risk_said(self, bench, monkeypatch) -> None:
        self._svd(monkeypatch)
        for name, value in (("device_id", 0x450), ("cpu", "Cortex-M7"),
                            ("device_name", "STM32H7xx"), ("board", "NUCLEO-H743ZI")):
            monkeypatch.setattr(_FakeProbe, name, value)
        monkeypatch.setattr(_FakeProbe, "memory", {0x58024400: 0x00000083})
        out = call("read_memory", address=None, register="RCC.CR")
        assert_contract(out)
        assert out["status"] == result.VERIFIED, out
        assert out["value"] == "0x00000083" and out["fields"]["HSION"] == 1
        assert out["svd"] == "STM32H743.svd" and "NUCLEO-H743ZI" in out["svd_source"]
        assert "No effect of reading this is known" in out["read_side_effects_note"]

    def test_a_read_the_file_marks_is_refused(self, bench, monkeypatch) -> None:
        self._svd(monkeypatch)
        monkeypatch.setattr(_FakeProbe, "device_name", "STM32N6xx")
        monkeypatch.setattr(_FakeProbe, "board", "NUCLEO-N657X0-Q")
        out = call("read_memory", address=None, register="ETH.MACISR")
        assert_contract(out)
        assert out["status"] == result.FAILED and "as ST's SVD file marks it" in out["error"]
        assert out["read_effects"][0]["provenance"] == "vendor-db"


class TestReadsThatChangeStateAreRefused:
    """A read the list in read_effects.py, ST's readAction or the user's board
    file says changes state is refused unless read_effects_ok; what each listed
    read does was measured on an STM32H743 and an STM32F407 on 2026-10-03. These
    use the installed SVD files, and skip without them."""

    H7 = {"device_id": 0x450, "cpu": "Cortex-M7", "device_name": "STM32H7xx", "board": "NUCLEO-H743ZI"}

    def _h7(self, monkeypatch, vendor: dict | None = None) -> list:
        TestReadingByName._svd(self, monkeypatch)
        for name, value in self.H7.items():
            monkeypatch.setattr(_FakeProbe, name, value)
        if vendor is not None:
            monkeypatch.setattr(server.svd_mod, "vendor_marks", lambda directory: vendor)
        reads: list = []
        real = _FakeProbe.read_memory

        def recording(probe, address, length_bytes):
            reads.append((address, length_bytes))
            return real(probe, address, length_bytes)

        monkeypatch.setattr(_FakeProbe, "read_memory", recording)
        return reads

    def test_a_uart_receive_register_in_a_range_is_not_read(self, bench, monkeypatch) -> None:
        reads = self._h7(monkeypatch, vendor={})
        out = call("read_memory", address="0x40004800", length_bytes=0x30)
        assert_contract(out)
        assert out["status"] == result.FAILED and reads == [], "refused before anything was read"
        assert "USART3.RDR" in out["error"] and "read_effects_ok" in out["error"]
        assert out["read_effects"][0]["provenance"] == "measured" and "USART3.ISR" in out["registers_read"]

    def test_with_read_effects_ok_it_is_read_and_says_what_happened(self, bench, monkeypatch) -> None:
        reads = self._h7(monkeypatch, vendor={})
        out = call("read_memory", address="0x40004824", length_bytes=4, read_effects_ok=True)
        assert_contract(out)
        assert out["status"] == result.VERIFIED and reads == [(0x40004824, 4)]
        assert out["read_effects"][0]["register"] == "USART3.RDR" and "has happened" in out["read_effects_note"]

    def test_a_peripheral_register_with_nothing_known_is_read_and_named(self, bench, monkeypatch) -> None:
        self._h7(monkeypatch, vendor={})
        out = call("read_memory", address="0x58024400", length_bytes=4)
        assert out["status"] == result.VERIFIED and out["registers_read"] == ["RCC.CR"]
        assert "does not mean it has none" in out["read_side_effects_note"]

    def test_ram_is_read_without_looking_anything_up(self, bench, monkeypatch) -> None:
        self._h7(monkeypatch, vendor={})
        monkeypatch.setattr(server.svd_mod, "choose", lambda *a, **kw: pytest.fail("looked up an SVD file"))
        out = call("read_memory", address="0x24000000", length_bytes=8)
        assert out["status"] == result.VERIFIED
        assert not {"registers_read", "read_side_effects_note", "read_effects"} & set(out)

    @pytest.mark.parametrize("register, refused", [("ADC3.DR", True), ("ADC3.ISR", False), ("RTC.SSR", True)])
    def test_by_name_as_by_address(self, bench, monkeypatch, register, refused) -> None:
        reads = self._h7(monkeypatch, vendor={})
        out = call("read_memory", address=None, register=register)
        assert_contract(out)
        assert (out["status"] == result.FAILED) is refused and bool(reads) is not refused

    def test_without_an_svd_file_for_the_part_it_says_it_cannot_tell(self, bench, monkeypatch) -> None:
        self._h7(monkeypatch, vendor={})
        monkeypatch.setattr(_FakeProbe, "device_name", "STM32Q9xx")
        monkeypatch.setattr(_FakeProbe, "board", None)
        out = call("read_memory", address="0x40004824", length_bytes=4)
        assert out["status"] == result.VERIFIED and "is not known" in out["registers_note"]

    def test_a_board_file_entry_is_user_stated(self, bench, monkeypatch, tmp_path) -> None:
        from stlink_mcp import read_effects
        self._h7(monkeypatch, vendor={})
        board_file = tmp_path / "boards.toml"
        board_file.write_text('[[read_effects]]\nblock = "GPIO"\nregisters = ["IDR"]\n'
                              'effect = "starts a conversion on the board this was written for"\n')
        monkeypatch.setattr(read_effects, "_user", read_effects.load_user_effects(board_file))
        out = call("read_memory", address=None, register="GPIOA.IDR")
        assert out["status"] == result.FAILED and out["read_effects"][0]["provenance"] == "user-stated"

    @pytest.mark.parametrize("text, says", [
        ('[[read_effects]]\nblock = "GPIO"\nregisters = ["IDR"]\neffect = "x"\nprovenance = "measured"\n',
         "cannot carry a provenance"),
        ('[[read_effects]]\nblock = "GPIO("\nregisters = ["IDR"]\neffect = "x"\n', "not a regular expression"),
        ('read_effects = "GPIO"\n', "[[read_effects]] tables"),
    ])
    def test_a_bad_board_file_entry_is_refused_whole(self, tmp_path, text, says) -> None:
        from stlink_mcp import quirks, read_effects
        board_file = tmp_path / "boards.toml"
        board_file.write_text(text)
        with pytest.raises(quirks.BoardConfigError, match=re.escape(says)):
            read_effects.load_user_effects(board_file)


class TestRunUntilClaimsOnlyWhatTheChipSaid:
    """run_until's verdicts, with the GDB side faked. What the fakes return is
    what the H7, the F407 and the N6 gave on 2026-10-02: the server logs the
    Debug Fault Status Register at each stop, 0x3 for a breakpoint and 0x5 for a
    watchpoint; GDB alone cannot tell a watchpoint stop from any other."""

    ELF = str(Path(__file__).parent / "fixtures" / "elf" / "h7_vars_dwarf5.elf")

    class _Server:
        port = 61234
        attach = True
        interface = "swd"

    class _Session:
        closes: list[bool] = []

        def __init__(self, gdb, elf, port) -> None:
            pass

        def close(self, detach: bool) -> None:
            type(self).closes.append(detach)

    def _rig(self, monkeypatch, *, stop=None, mismatch=None, xpsr=0x01000000):
        from stlink_mcp import debugger
        type(self)._Session.closes = []
        monkeypatch.setattr(server, "_toolchain", lambda: SimpleNamespace(gdb=Path("arm-none-eabi-gdb"), root=Path("/")))
        type(self).dropped = []
        monkeypatch.setattr(server, "_debug_server", lambda t, s, i: (self._Server(), None, True))
        monkeypatch.setattr(server, "_drop_debug_server", lambda serial: type(self).dropped.append(serial))
        monkeypatch.setattr(server.gdbmi, "Session", self._Session)
        monkeypatch.setattr(server, "_elf_on_target", lambda session, elf: mismatch)
        monkeypatch.setattr(server.debugger_mod, "registers", lambda session: {"PC": 0x24000042, "XPSR": xpsr})
        monkeypatch.setattr(server.debugger_mod, "system_registers", lambda session: {"MSP": 0x2407FFD8})
        if stop is not None:
            monkeypatch.setattr(server.debugger_mod, "run_until", lambda session, srv, **kw: stop)
        monkeypatch.setattr(server, "_stopped", {})
        return debugger

    def _stop(self, debugger, *, reached=True, dfsr=(0x3,), at=True, watch=False):
        frames = [{"function": "writer", "file": "images/debug_target.c", "line": 39, "address": "0x24000042"}]
        detail = {"watched": {"expression": "g_watched"}} if watch else {"at_location": at}
        return debugger.Stop(reached, 0.4, 0x24000042, "breakpoint-hit", list(dfsr), frames, False, detail)

    def test_a_breakpoint_the_chip_names_is_verified_and_held(self, bench, monkeypatch) -> None:
        from stlink_mcp import debugger
        self._rig(monkeypatch, stop=self._stop(debugger))
        out = call("run_until", elf_path=self.ELF, location="writer")
        assert_contract(out)
        assert out["status"] == result.VERIFIED and "names a breakpoint (0x00000003)" in out["verified_by"]
        assert out["halted_during_call"] is True and "watchdogs" in out["core_after"] and out["target_was_reset"] is False
        assert out["interface"] == "swd", "N8: the result says which interface the stop went through"
        assert self._Session.closes == [False], "disconnected, leaving the core halted"

    @pytest.mark.parametrize("location, expected", [
        ("delay", (0x240000D6,)), ("writer", ()), ("debug_target.c:48", ()), ("0x24000042", ()),
    ])
    def test_a_functions_inlined_copies_are_asked_for_too(self, bench, monkeypatch, location, expected) -> None:
        """GDB breaks on a function's out-of-line copy only; this fixture's delay
        is also inlined into its caller at 0x240000D6, as on the N6 where
        run_until said "not reached" while the loop ran (2026-10-04)."""
        from stlink_mcp import debugger
        self._rig(monkeypatch)
        asked: dict = {}
        stop = self._stop(debugger)
        monkeypatch.setattr(server.debugger_mod, "run_until", lambda session, srv, **kw: asked.update(kw) or stop)
        call("run_until", elf_path=self.ELF, location=location)
        assert asked["also_at"] == expected

    def test_a_watchpoint_says_the_write_came_before(self, bench, monkeypatch) -> None:
        from stlink_mcp import debugger
        self._rig(monkeypatch, stop=self._stop(debugger, dfsr=(0x5,), watch=True))
        out = call("run_until", elf_path=self.ELF, location=None, watch="g_watched")
        assert out["status"] == result.VERIFIED and "watchpoint" in out["verified_by"]
        assert "before this stop" in out["write_note"]

    @pytest.mark.parametrize("kw, says", [
        ({"reached": False}, "was not reached in"),
        ({"dfsr": ()}, "no stop reason for it"),
    ])
    def test_what_the_chip_did_not_say_is_unverified(self, bench, monkeypatch, kw, says) -> None:
        from stlink_mcp import debugger
        self._rig(monkeypatch, stop=self._stop(debugger, **kw))
        out = call("run_until", elf_path=self.ELF, location="writer")
        assert_contract(out)
        assert out["status"] == result.UNVERIFIED and says in out["unverified_because"]

    def test_the_server_holding_the_probe_is_used_whatever_its_kind(self, bench, monkeypatch) -> None:
        """Every server keeps its log, so run_until never swaps run_firmware's for
        one of its own: a swap let go of the probe for a moment, which a program
        that closes the debug port would not give back, and on the N6 the kept
        server's stops were unverified for want of the log (2026-10-04)."""
        held = SimpleNamespace(interface="swd", attach=False)
        monkeypatch.setattr(server, "held_server", lambda serial: held)
        monkeypatch.setattr(server, "release_server", lambda serial=None: pytest.fail("let go of the probe"))
        assert server._debug_server(object(), "AAA1", "swd") == (held, None, False)

    def test_gdb_that_cannot_reach_the_core_says_what_does_that(self, bench, monkeypatch) -> None:
        """On an H7 whose program had taken the SWD pins, run_until came back with
        GDB's bare timeout, the message below (2026-10-04)."""
        self._rig(monkeypatch)

        def no_answer(*args, **kwargs):
            raise server.gdbmi.MiError("-target-select extended-remote localhost:61238: no answer from GDB within 30 s")

        monkeypatch.setattr(server.gdbmi, "Session", no_answer)
        out = call("run_until", elf_path=self.ELF, location="writer")
        assert_contract(out)
        assert out["status"] == result.FAILED and "SWD pins" in out["next_step"] and "release_probe" in out["next_step"]

    def test_interrupted_in_a_fault_handler_points_at_the_fault_tools(self, bench, monkeypatch) -> None:
        from stlink_mcp import debugger
        self._rig(monkeypatch, stop=self._stop(debugger, reached=False), xpsr=0x01000003)
        out = call("run_until", elf_path=self.ELF, location="writer")
        assert out["status"] == result.UNVERIFIED and "HardFault" in out["unverified_because"]
        assert out["in_exception"].startswith("3 ")

    def test_a_stop_somewhere_else_is_failed(self, bench, monkeypatch) -> None:
        from stlink_mcp import debugger
        self._rig(monkeypatch, stop=self._stop(debugger, at=False, dfsr=(0x1,)))
        out = call("run_until", elf_path=self.ELF, location="writer")
        assert out["status"] == result.FAILED and "not at writer" in out["error"]

    def test_another_program_on_the_target_is_refused_and_resumed(self, bench, monkeypatch) -> None:
        self._rig(monkeypatch, mismatch="the target is not running this ELF")
        out = call("run_until", elf_path=self.ELF, location="writer")
        assert out["status"] == result.FAILED and "another program's code" in out["error"]
        assert self._Session.closes == [True], "detached, so the core it halted runs on"
        assert self.dropped, "the server it started is stopped, leaving the probe as it was"

    @pytest.mark.parametrize("args", [{"location": None}, {"watch": "g_watched"}])
    def test_one_of_location_or_watch(self, bench, args) -> None:
        out = call("run_until", elf_path=self.ELF, **args)
        assert out["status"] == result.FAILED and "one of location or watch" in out["error"]

    @pytest.mark.parametrize("fails_in", ["run_until", "registers"])
    def test_a_gdb_error_midway_resumes_the_core_and_says_so(self, bench, monkeypatch, fails_in) -> None:
        """GDB halts the core when it attaches. An error after that, before the
        stop or after it, disconnected and left the core halted, and the server
        this call started held, with a failure that said neither (N4)."""
        from stlink_mcp import debugger
        self._rig(monkeypatch, stop=self._stop(debugger))

        def gdb_error(*a, **kw):
            raise server.gdbmi.MiError("-break-list: Remote connection closed")

        monkeypatch.setattr(server.debugger_mod, fails_in, gdb_error)
        out = call("run_until", elf_path=self.ELF, location="writer")
        assert_contract(out)
        assert out["status"] == result.FAILED and "Remote connection closed" in out["error"]
        assert self._Session.closes == [True], "detached, so the core runs on"
        assert self.dropped, "the server it started is stopped"
        assert out["core_after"].startswith("resumed") and server._stopped == {}

    def test_a_server_held_over_another_interface_is_not_reused(self, bench, monkeypatch) -> None:
        """run_until and core_registers reused a held JTAG server for a call that
        asked for SWD, through the second ST-LINK on the H7 on 2026-10-03, and
        said nothing of it; run_firmware already refused that."""
        jtag = SimpleNamespace(port=61234, attach=True, interface="jtag")
        monkeypatch.setattr(server, "_toolchain", lambda: SimpleNamespace(gdb=Path("arm-none-eabi-gdb"), root=Path("/")))
        monkeypatch.setattr(server, "held_server", lambda serial: jtag)
        monkeypatch.setattr(server.gdbmi, "Session", lambda *a, **kw: pytest.fail("a GDB session was started"))
        for out in (call("run_until", elf_path=self.ELF, location="writer", interface="swd"),
                    call("core_registers", elf_path=self.ELF, interface="swd")):
            assert_contract(out)
            assert out["status"] == result.FAILED and "uses JTAG, and this call asked for SWD" in out["error"]

    def test_core_registers_leaves_a_stopped_core_halted(self, bench, monkeypatch) -> None:
        from stlink_mcp import debugger
        self._rig(monkeypatch, stop=self._stop(debugger))
        monkeypatch.setattr(server, "held_server", lambda serial: self._Server())
        monkeypatch.setattr(server.debugger_mod, "frames", lambda session, depth=8: ([{"function": "writer"}], False))
        call("run_until", elf_path=self.ELF, location="writer")
        out = call("core_registers", elf_path=self.ELF)
        assert_contract(out)
        assert out["status"] == result.VERIFIED and out["call_stack"] == [{"function": "writer"}]
        assert out["core_after"].startswith("still halted") and out["interface"] == "swd"
        assert self._Session.closes == [False, False]

    @pytest.mark.parametrize("halted, closes, says", [
        (True, [False], "still halted, as it was found"), (False, [True], "running"),
    ])
    def test_core_registers_leaves_the_core_as_it_found_it(self, bench, monkeypatch, halted, closes, says) -> None:
        """GDB's detach resumes the core, and a register read set one the held
        server had halted at its reset vector running into its flash program
        (agent-use evaluation, 2026-10-04)."""
        self._rig(monkeypatch)
        monkeypatch.setattr(server, "held_server", lambda serial: self._Server())
        monkeypatch.setattr(server, "_halted_now", lambda toolchain, serial, interface: halted)
        monkeypatch.setattr(server, "_core_after_observed", lambda *a: "running: DHCSR 0x01010001")
        out = call("core_registers")
        assert out["core_after"].startswith(says) and self._Session.closes == closes


class TestWithoutUdevTheConsoleIsFoundByUsbSerial:
    """On Windows, which has no /dev/serial/by-id, pyserial reported the
    ST-LINK's serial for its COM port: COM3, 003E00463234510337333934, an
    STLINK-V3 on Windows 11, 2026-10-02. The port objects here carry what it
    reported."""

    SERIAL = "003E00463234510337333934"

    def _ports(self, monkeypatch, *ports):
        from serial.tools import list_ports
        monkeypatch.setattr(list_ports, "comports", lambda *a, **k: [
            SimpleNamespace(device=device, serial_number=serial) for device, serial in ports
        ])

    def test_the_port_with_the_probes_serial_is_its_console(self, bench, monkeypatch) -> None:
        self._ports(monkeypatch, ("COM3", self.SERIAL), ("COM7", None))
        def no_listing(tc):
            raise AssertionError("a named probe's console must not need a listing")
        monkeypatch.setattr(server, "_list_probes", no_listing)
        bench.console_text = "tick 0\n"
        out = call("capture_console", probe_serial=self.SERIAL)
        assert_contract(out)
        assert out["console_port"] == "COM3" and out["console_port_source"] == "USB serial number"

    def test_two_ports_with_one_serial_are_not_chosen_between(self, bench, monkeypatch) -> None:
        self._ports(monkeypatch, ("COM3", self.SERIAL), ("COM4", self.SERIAL.lower()))
        out = call("capture_console", probe_serial=self.SERIAL)
        assert out["status"] == result.FAILED and "COM3, COM4" in out.get("console_note", "")


class TestAddressesAndSizesAreCheckedAtTheBoundary:
    """JSON has no hex literals, so an agent had to turn "0x20000000" into
    536870912 itself, and a slip read or wrote somewhere else. Sizes and
    durations had no bounds, so one call could flood the agent's context or,
    with calls serialised, hold up every other tool for as long as it liked."""

    def test_a_hex_string_is_an_address(self, bench) -> None:
        out = call("read_memory", address="0x20000000", length_bytes=8)
        assert_contract(out)
        assert out["status"] == result.VERIFIED
        assert list(out["words"]) == ["0x20000000", "0x20000004"]

    @pytest.mark.parametrize("address,why", [
        ("08000000", "0x prefix"), ("0xZZ", "0x prefix"), (-4, "negative"),
        (0x1_0000_0000, "32-bit"), (0x20000002, "aligned"),
    ])
    def test_an_unusable_address_is_refused(self, bench, address, why: str) -> None:
        out = call("read_memory", address=address)
        assert_contract(out)
        assert out["status"] == result.FAILED
        assert why in out["error"]

    @pytest.mark.parametrize("length", [0, 6, 2048])
    def test_an_unusable_length_is_refused(self, bench, length: int) -> None:
        out = call("read_memory", length_bytes=length)
        assert out["status"] == result.FAILED

    def test_a_short_read_is_not_verified(self, bench, monkeypatch) -> None:
        """The words that came back are reported; the rest are named as missing
        rather than left out of a verified dict."""
        monkeypatch.setattr(
            _FakeProbe, "read_memory", lambda self, addr, n: {addr: 0x11111111}
        )
        out = call("read_memory", address=0x20000000, length_bytes=16)
        assert_contract(out)
        assert out["status"] == result.FAILED
        assert out["words"] == {"0x20000000": "0x11111111"}
        assert out["missing"] == ["0x20000004", "0x20000008", "0x2000000C"]

    def test_flash_firmware_takes_a_hex_string_as_the_callers_address(
        self, bench, monkeypatch
    ) -> None:
        from stlink_mcp import quirks as quirks_mod

        seeded = quirks_mod.FamilyQuirks(
            name="STM32F4", access_port=0, flash_base=0x08000000,
            default_provenance=quirks_mod.VENDOR_DB,
        )
        monkeypatch.setattr(probe_mod, "for_device_id", lambda did: seeded)
        out = call("flash_firmware", image_path=str(bench.bin), address="0x08000000")
        assert out["status"] == result.VERIFIED
        assert out["address"] == "0x08000000"

    @pytest.mark.parametrize("seconds", [0, -1, 61, float("inf")])
    def test_capture_time_is_bounded(self, bench, seconds: float) -> None:
        assert call("capture_console", seconds=seconds)["status"] == result.FAILED
        out = call("run_firmware", elf_path=str(bench.elf), console_seconds=seconds)
        assert out["status"] == result.FAILED
        assert "console_seconds" in out["error"]


class TestReleaseProbe:
    def test_releasing_nothing_is_not_reported_as_a_release(self) -> None:
        """It returned a hardcoded ok=True regardless of whether a session
        existed, so 'released' and 'ok' could disagree."""
        server._held.clear()
        r = call("release_probe", probe_serial="never-held-this")
        assert_contract(r)
        assert r["ok"] is False
        assert r["released"] is False


class TestTheWritePathWillNotActOnAnUnmeasuredAddress:
    """`flash_firmware` is the one tool whose mistake is not undone by a reset.

    Everywhere else this server may act on an address nobody has measured and
    let the observation settle it, because a RAM load that used the wrong base
    is reversible and there IS an observation: the target either produces
    console output or it does not. Neither holds here. This tool writes
    persistently, has no readback, and the only check available is the vendor's
    own completion banner, which is the specific claim this project exists to
    distrust.

    So the refusal is a property of the write path rather than a property of the
    table being empty, and it has to survive device coverage arriving. These
    tests use a device whose flash base is present and merely unmeasured, which
    is exactly what a seeded entry looks like.
    """

    UNMEASURED_DEVICE = 0x431  # STM32F411, not in FAMILIES: a plausible seeded part

    @pytest.fixture
    def seeded_bench(self, bench, monkeypatch):
        from stlink_mcp import quirks as quirks_mod

        seeded = quirks_mod.FamilyQuirks(
            name="STM32F4",
            access_port=0,
            ram_load_base=0x20000000,
            vector_table_addr=0x20000000,
            flash_base=0x08000000,
            notes="seeded from the vendor device database, nothing measured",
            default_provenance=quirks_mod.VENDOR_DB,
            provenance={"access_port": quirks_mod.ASSUMED},
        )
        monkeypatch.setattr(probe_mod, "for_device_id", lambda did: seeded)
        monkeypatch.setattr(_FakeProbe, "device_id", self.UNMEASURED_DEVICE)
        return bench

    def test_it_refuses_a_flash_base_nobody_measured(self, seeded_bench, tmp_path) -> None:
        image = tmp_path / "image.bin"
        image.write_bytes(b"\x00" * 16)
        out = server.flash_firmware(image_path=str(image))
        assert out["ok"] is False
        assert out["status"] == result.FAILED
        assert "vendor-db" in out["error"]

    def test_the_refusal_says_what_to_do_instead(self, seeded_bench, tmp_path) -> None:
        """A refusal an agent cannot act on becomes a retry loop."""
        image = tmp_path / "image.bin"
        image.write_bytes(b"\x00" * 16)
        out = server.flash_firmware(image_path=str(image))
        assert "address" in out.get("hint", "")

    def test_an_explicit_address_is_the_callers_claim_and_is_allowed(
        self, seeded_bench, tmp_path
    ) -> None:
        """The refusal is about the TABLE guessing, not about the operation.

        A caller who names the address has made the claim themselves, and
        blocking that would leave no way to flash a part this project has not
        characterised, which is worse and is not what N2 asks for.
        """
        image = tmp_path / "image.bin"
        image.write_bytes(b"\x00" * 16)
        out = server.flash_firmware(image_path=str(image), address=0x08000000)
        assert out["status"] != result.FAILED or "vendor-db" not in out.get("error", "")


class TestFlashFirmwareTakesOnlyWhatItCanPlace:
    """The programmer's `-w <file> <address>` uses the address for a raw binary
    only, by ST's manual; HEX, S-record and ELF files are written to the
    addresses inside them. So for those formats the check that the flash base
    was measured protected nothing, and the `address` in a verified result need
    not be where the write went."""

    @pytest.fixture
    def no_connect(self, bench, monkeypatch):
        def must_not_connect(self, allow_reset=True):
            raise AssertionError("the probe was touched for a refused image")

        monkeypatch.setattr(_FakeProbe, "identify", must_not_connect)
        return bench

    @pytest.mark.parametrize("name", ["image.elf", "image.hex", "image.srec", "image"])
    def test_anything_but_a_bin_is_refused_before_connecting(
        self, no_connect, tmp_path, name: str
    ) -> None:
        path = tmp_path / name
        path.write_bytes(b":020000040800F2\n")
        out = call("flash_firmware", image_path=str(path))
        assert_contract(out)
        assert out["status"] == result.FAILED
        assert "raw binary" in out["error"]

    def test_an_elf_named_bin_is_refused(self, no_connect, tmp_path) -> None:
        from elfgen import minimal_elf32
        path = tmp_path / "renamed.bin"
        path.write_bytes(minimal_elf32(0x08000000, 0x100))
        out = call("flash_firmware", image_path=str(path))
        assert out["status"] == result.FAILED
        assert "is an ELF file" in out["error"]

    def test_a_missing_or_empty_image_is_refused(self, no_connect, tmp_path) -> None:
        assert "No such image" in call(
            "flash_firmware", image_path=str(tmp_path / "absent.bin"))["error"]
        empty = tmp_path / "empty.bin"
        empty.write_bytes(b"")
        assert "empty" in call("flash_firmware", image_path=str(empty))["error"]

    def test_a_bin_is_written_and_the_result_names_what_was_sent(self, bench) -> None:
        out = call("flash_firmware", image_path=str(bench.bin))
        assert out["status"] == result.VERIFIED
        assert out["image_size_bytes"] == 16
        assert out["address"] == "0x70000000"  # the fake board is the measured N6
        assert "compared them with the image: identical" in out["verified_by"]


class TestResetIsNotClaimedUnlessItWasSeen:
    """N7 says reset is a first-class operation; N1 says it is not reported as
    working unless something observed it.

    The three shapes here are the three real hardware produces, and all three
    were measured on 2026-08-16 rather than imagined:

      landed on the vector   NUCLEO-H743ZI2, reset handler 0x0807FA98, reached
                             exactly by a HWRSTPULSE connect
      landed elsewhere       the same board via `-rst -halt`, which resets fine
                             and then loses the race, ending at 0x0807FC2C
      no answer at all       a debug port that does not respond after the
                             pulse, which on the STM32N6 means it was almost
                             certainly closed already

    The third one was nearly recorded as "the reset closes the port", from one
    pulsed board and one untouched board that differed. It does not: a
    NUCLEO-N657X0-Q confirmed open, then pulsed, halted in its boot ROM at
    0x18003A1A with the port still open, because a reset-and-halt catches the
    core before any image runs. The tool therefore claims nothing about cause
    in that branch, and this note is here so nobody re-derives the wrong one.
    """

    @pytest.fixture
    def h7_bench(self, bench, monkeypatch):
        """A part that boots from internal flash, so its post-reset PC is
        predictable and the check can actually run."""
        from stlink_mcp import quirks as quirks_mod

        h7 = quirks_mod.FamilyQuirks(
            name="STM32H7", access_port=0, ram_load_base=0x24000000,
            vector_table_addr=0x24000000, flash_base=0x08000000,
            requires_signing=False, debug_closes_on_run=False, notes="x",
        )
        monkeypatch.setattr(probe_mod, "for_device_id", lambda did: h7)
        # Word 1 of the vector table: the reset handler, Thumb bit set.
        monkeypatch.setattr(
            _FakeProbe, "read_memory",
            lambda self, addr, n: {addr: 0x20020000, addr + 4: 0x0807FA99},
        )
        return bench

    def test_landing_on_the_reset_vector_is_a_verified_reset(
        self, h7_bench, monkeypatch
    ) -> None:
        monkeypatch.setattr(_FakeProbe, "reset_pc", 0x0807FA98)
        out = call("reset_target")
        assert out["ok"] is True
        assert "0x0807FA98" in out["verified_by"]

    def test_the_thumb_bit_does_not_break_the_comparison(self, h7_bench, monkeypatch) -> None:
        """The vector table stores 0x0807FA99 and the PC reads back 0x0807FA98.

        Comparing them unmasked is false for every correct reset, which is worse
        than no check: it reports a reset that did happen as one that did not,
        and sends someone to debug working hardware.
        """
        monkeypatch.setattr(_FakeProbe, "reset_pc", 0x0807FA98)
        assert call("reset_target")["ok"] is True

    def test_landing_anywhere_else_is_not_a_reset(self, h7_bench, monkeypatch) -> None:
        """The `-rst -halt` shape: the part really did reset, and the PC still
        cannot be used to say so."""
        monkeypatch.setattr(_FakeProbe, "reset_pc", 0x0807FC2C)
        out = call("reset_target")
        assert out["ok"] is False
        assert "0x0807FC2C" in out["unverified_because"]

    def test_a_reset_that_closed_the_port_is_unverified_not_successful(
        self, h7_bench, monkeypatch
    ) -> None:
        monkeypatch.setattr(_FakeProbe, "reset_pc", None)
        out = call("reset_target")
        assert out["ok"] is False
        assert out["status"] == result.UNVERIFIED
        assert "did not answer" in out["unverified_because"]

    def test_an_unpredictable_part_says_so_rather_than_guessing(self, bench, monkeypatch) -> None:
        """The fake board is an STM32N6: no internal flash, so it comes up in a
        boot ROM at an address the table does not record. Nothing to compare
        against is a reason to report unverified, not a reason to invent one."""
        monkeypatch.setattr(_FakeProbe, "reset_pc", 0x34180400)
        out = call("reset_target")
        assert out["ok"] is False
        assert "not predictable" in out["unverified_because"]

    def test_the_core_is_released_and_the_result_says_so(self, h7_bench, monkeypatch) -> None:
        """N4: the halt was not asked for, so it is reported."""
        monkeypatch.setattr(_FakeProbe, "reset_pc", 0x0807FA98)
        out = call("reset_target")
        assert out["core_after"].startswith("running")
        assert "resumed" in out["core_after"]

    def test_it_does_not_claim_whether_the_core_is_halted_now(
        self, h7_bench, monkeypatch
    ) -> None:
        """There was a `leave_halted` option and a `core_halted` field for a day.
        Both were removed after hardware showed the claim was false: a halt made
        through the programmer does not outlive the invocation that made it, so
        `core_halted: true` was reported while the core was running. And nothing
        here can observe the answer either way, since a static program counter is
        what a halted core and a tight loop both look like."""
        monkeypatch.setattr(_FakeProbe, "reset_pc", 0x0807FA98)
        out = call("reset_target")
        assert "core_halted" not in out
        assert out["halted_during_call"] is True
        assert out["core_after"].startswith("running")


class TestResetTargetTellsTheTruthAboutTheCore:
    """Regression tests for the two defects a red-team found in the first
    version of `reset_target`, both of which had run on real hardware.

    The first: `core_halted` was computed once, before the verdict branches, as
    `pc is not None and leave_halted` - False by default - and the two
    unverified returns executed before the core was ever released. So every
    unverified outcome left the core physically halted and reported that it was
    not, and one payload managed to say "the core halted at 0x..." in prose and
    `core_halted: false` in a field beside it.

    The second: a release that failed was reported inside a `verified` envelope
    with the failure demoted to a note.
    """

    @pytest.fixture
    def h7_bench(self, bench, monkeypatch):
        from stlink_mcp import quirks as quirks_mod

        h7 = quirks_mod.FamilyQuirks(
            name="STM32H7", access_port=0, ram_load_base=0x24000000,
            vector_table_addr=0x24000000, flash_base=0x08000000,
            requires_signing=False, debug_closes_on_run=False, notes="x",
        )
        monkeypatch.setattr(probe_mod, "for_device_id", lambda did: h7)
        monkeypatch.setattr(
            _FakeProbe, "read_memory",
            lambda self, addr, n: {addr: 0x20020000, addr + 4: 0x0807FA99},
        )
        return bench

    def test_an_unverifiable_reset_still_releases_the_core(self, bench, monkeypatch) -> None:
        """The STM32N6 path, exactly as it ran on hardware: the core halts in the
        boot ROM and the reset cannot be verified. The old code returned before
        releasing it, and reported core_halted false while it was stopped."""
        monkeypatch.setattr(_FakeProbe, "reset_pc", 0x34180400)
        out = call("reset_target")
        assert out["ok"] is False
        assert out["core_after"].startswith("running"), (
            "the core should have been RELEASED even though the reset could not "
            "be verified: the two are independent facts"
        )

    def test_a_release_failure_is_not_a_verified_reset(self, h7_bench, monkeypatch) -> None:
        def boom(self, access_port):
            raise RuntimeError("the programmer did not confirm the core resumed")

        monkeypatch.setattr(_FakeProbe, "reset_pc", 0x0807FA98)
        monkeypatch.setattr(_FakeProbe, "release_core", boom)
        out = call("reset_target")
        assert out["ok"] is False
        assert out["status"] == result.FAILED
        assert not out["core_after"].startswith("running")
        # The reset itself WAS observed, and that evidence is not thrown away.
        assert "0x0807FA98" in out["reset_verified_by"]

    def test_a_core_that_is_halted_is_released_on_every_path(
        self, h7_bench, monkeypatch
    ) -> None:
        """Whether the reset could be verified has nothing to do with whether
        the caller asked for a running target."""
        released: list[int] = []
        monkeypatch.setattr(
            _FakeProbe, "release_core",
            lambda self, ap: released.append(ap) or "Core run\n",
        )
        for pc in (0x0807FA98, 0x0807FC2C):   # verified, then landed-elsewhere
            released.clear()
            monkeypatch.setattr(_FakeProbe, "reset_pc", pc)
            out = call("reset_target")
            assert released, f"core left halted for pc={pc:#x}"
            assert out["core_after"].startswith("running")

    def test_nothing_is_released_when_nothing_was_halted(self, h7_bench, monkeypatch) -> None:
        released: list[int] = []
        monkeypatch.setattr(_FakeProbe, "reset_pc", None)
        monkeypatch.setattr(
            _FakeProbe, "release_core",
            lambda self, ap: released.append(ap) or "Core run\n",
        )
        out = call("reset_target")
        assert not released
        assert out["halted_during_call"] is False
        assert not out["core_after"].startswith("running")
        assert out["ok"] is False

    def test_every_unverified_branch_offers_a_next_step(self, h7_bench, monkeypatch) -> None:
        """An agent whose only source of information is the result cannot act on
        a bare statement of fact."""
        for pc in (None, 0x0807FC2C):
            monkeypatch.setattr(_FakeProbe, "reset_pc", pc)
            out = call("reset_target")
            assert out.get("hint"), f"no hint on the branch for pc={pc}"


class TestCoreRegistersDoesNotClaimTheCoreStaysHalted:
    """The same defect reset_target had, left behind in a sibling tool.

    On 2026-08-17 two H7 boards showed that a halt made through the programmer
    does not outlive the command that made it, and reset_target stopped
    reporting `core_halted`. core_registers went on returning `"halted": true`
    and "The core is now halted", so an agent was told the core was stopped
    while, on the boards measured, it had already resumed.
    """

    def test_no_field_or_note_claims_the_core_is_halted_now(self, bench) -> None:
        out = call("core_registers")
        assert_contract(out)
        assert "halted" not in out
        assert out["halted_during_call"] is True
        assert "now halted" not in out["core_after"]

    def test_what_the_core_does_afterwards_is_read_not_assumed(self, bench, monkeypatch) -> None:
        """DHCSR read without halting after the call. Agents in an evaluation took
        "not observed" as leaving them unsure whether the core ran."""
        monkeypatch.setattr(_FakeProbe, "memory", {0xE000EDF0: 0x01010000})
        assert call("core_registers")["core_after"].startswith("running: DHCSR 0x01010000")
        monkeypatch.setattr(_FakeProbe, "memory", {0xE000EDF0: 0x00030003})
        assert call("core_registers")["core_after"].startswith("halted: DHCSR 0x00030003")

    def test_when_that_read_fails_it_says_what_was_measured_instead(self, bench, monkeypatch) -> None:
        def unreadable(self, address, length_bytes):
            if address == 0xE000EDF0:
                raise probe_mod.ProbeError("no answer")
            return {address: 0}
        monkeypatch.setattr(_FakeProbe, "read_memory", unreadable)
        assert call("core_registers")["core_after"].startswith("not observed")


class TestReleaseCoreChecksItsOwnEffect:
    def test_it_raises_when_the_programmer_does_not_confirm(self, monkeypatch) -> None:
        """run_command does not raise on a nonzero exit or an error banner, so
        an unchecked release returns cleanly while the core is still stopped."""
        from stlink_mcp import probe as pm
        from stlink_mcp.toolchain import CommandResult

        monkeypatch.setattr(
            pm, "run_command",
            lambda argv, timeout=90: CommandResult(argv, 0, "Error: cannot connect\n"),
        )
        p = pm.Probe(toolchain=SimpleNamespace(programmer="x"), serial="AAA")
        with pytest.raises(pm.ProbeError, match="still HALTED"):
            p.release_core(0)

    def test_it_accepts_the_confirmation_the_cli_actually_prints(self, monkeypatch) -> None:
        """Measured on a NUCLEO-H743ZI2: the CLI prints "Core run"."""
        from stlink_mcp import probe as pm
        from stlink_mcp.toolchain import CommandResult

        monkeypatch.setattr(
            pm, "run_command",
            lambda argv, timeout=90: CommandResult(argv, 0, "Core run\n"),
        )
        p = pm.Probe(toolchain=SimpleNamespace(programmer="x"), serial="AAA")
        assert "Core run" in p.release_core(0)


class TestAnUnmeasuredAccessPortIsAnnounced:
    """RT1: the ASSUMED provenance existed and changed nothing.

    Its docstring said an assumed port "must not be trusted the way a measured
    one is" and called that the signal the scan needs. Nothing read access-port
    provenance anywhere: the only two call sites of is_measured/provenance_of in
    the whole server were for flash_base. A reader checking whether the
    provenance model protected the access port found text saying it did.

    The trap it must warn about, from this project's own history: on the STM32N6
    AP0 reports device ID, name, CPU and revision perfectly well while being
    unable to halt the core, so a wrong port surfaces much later as "Failed to
    halt target", which reads as broken hardware.
    """

    def _probe_with(self, monkeypatch, answers: dict[int, bool]):
        from stlink_mcp import probe as pm
        from stlink_mcp.toolchain import CommandResult

        def fake_run(argv, timeout=90):
            port = next(int(a.split("=")[1]) for a in argv if a.startswith("ap="))
            body = "Device ID   : 0x999\n" if answers.get(port) else "Error: Unable to get core ID\n"
            return CommandResult(argv, 0, body)

        monkeypatch.setattr(pm, "run_command", fake_run)
        return pm.Probe(toolchain=SimpleNamespace(programmer="x"), serial="AAA")

    def test_an_unknown_device_says_its_port_was_never_measured(self, monkeypatch) -> None:
        probe = self._probe_with(monkeypatch, {0: True, 1: False})
        probe.identify()
        assert probe.access_port_provenance == "assumed"
        assert "nobody measured" in probe.unmeasured_port_warning
        assert probe.other_ports_that_answered == []

    def test_it_names_the_other_port_when_one_also_answers(self, monkeypatch) -> None:
        """The N6 shape: both ports answer identity and only one can halt. That
        is the single most useful fact available without perturbing the target,
        and it is the case the warning exists for."""
        probe = self._probe_with(monkeypatch, {0: True, 1: True})
        probe.identify()
        assert probe.other_ports_that_answered == [1]
        assert "AP1 also answered" in probe.unmeasured_port_warning

    def test_a_measured_family_pays_nothing_and_warns_nothing(self, monkeypatch) -> None:
        """The extra connect happens only for a part whose port was never
        measured, so a characterised family is unaffected."""
        from stlink_mcp import probe as pm
        from stlink_mcp.toolchain import CommandResult

        calls: list[int] = []

        def fake_run(argv, timeout=90):
            port = next(int(a.split("=")[1]) for a in argv if a.startswith("ap="))
            calls.append(port)
            return CommandResult(argv, 0, "Device ID   : 0x450\n")

        monkeypatch.setattr(pm, "run_command", fake_run)
        probe = pm.Probe(toolchain=SimpleNamespace(programmer="x"), serial="AAA")
        probe.identify()
        assert probe.access_port_provenance == "measured"
        assert probe.unmeasured_port_warning is None
        assert calls == [0], "a measured family must not pay for the extra probe"

    def test_board_state_surfaces_the_warning(self, bench, monkeypatch) -> None:
        monkeypatch.setattr(_FakeProbe, "access_port_provenance", "assumed")
        monkeypatch.setattr(_FakeProbe, "unmeasured_port_warning", "the port is a guess")
        monkeypatch.setattr(_FakeProbe, "other_ports_that_answered", [1])
        out = call("board_state")
        assert out["access_port_provenance"] == "assumed"
        assert out["access_port_warning"] == "the port is a guess"
        assert out["other_access_ports_that_answered"] == [1]

    def test_board_state_reports_the_value_beside_its_provenance(self, bench) -> None:
        """RT7: provenance_summary named debug_closes_on_run and the payload
        carried no key holding it, so an agent was told where a fact came from
        without being told the fact."""
        out = call("board_state")
        family = out["family"]
        for name in family["provenance"]:
            assert name in family or name == "access_port", (
                f"{name} has a provenance in the payload but no value beside it"
            )


class TestAResettingConnectIsReportedWhetherOrNotItConnects:
    """N4, through the real identify() rather than _FakeProbe.

    A HWRSTPULSE connect pulses nRST before it asks the debug port to answer;
    reset_target's "the pulse was issued, but the debug port did not answer"
    branch rests on exactly that. identify() and _settle_access_port set
    target_was_reset only when the resetting connect SUCCEEDED, so on an STM32N6
    whose image had closed AP1, board_state pulsed the board, failed to reach
    AP1, and reported target_was_reset False. The contract suite could not see
    it, because every tool test there replaces Probe with a fake that never
    resets.
    """

    def _cli(self, monkeypatch, answers: dict[tuple[str, int], int]):
        """A fake programmer. `answers` maps (connect mode, access port) to the
        device ID that connect reports; anything absent fails the way a closed
        port does. Returns the list of connects issued, in order."""
        from stlink_mcp import probe as pm
        from stlink_mcp.toolchain import CommandResult

        issued: list[tuple[str, int]] = []

        def fake_run(argv, timeout=90):
            ap = next(int(a.split("=")[1]) for a in argv if a.startswith("ap="))
            mode = next(a.split("=")[1] for a in argv if a.startswith("mode="))
            issued.append((mode, ap))
            device = answers.get((mode, ap))
            body = (f"Device ID   : 0x{device:03X}\n" if device is not None
                    else "Error: Unable to get core ID\n")
            return CommandResult(argv, 0, body)

        monkeypatch.setattr(pm, "run_command", fake_run)
        return issued

    def _probe(self):
        from stlink_mcp import probe as pm
        return pm.Probe(toolchain=SimpleNamespace(programmer="x"), serial="AAA1")

    def test_a_pulse_that_fails_to_reopen_the_family_port_is_still_a_reset(
        self, monkeypatch
    ) -> None:
        """The STM32N6 with AP1 closed by its image: AP0 identifies, AP1 does not
        answer even to a resetting connect."""
        issued = self._cli(monkeypatch, {("HOTPLUG", 0): 0x486})
        probe = self._probe()
        probe.identify()
        assert ("HWRSTPULSE", 1) in issued
        assert probe.degraded_reason
        assert probe.target_was_reset is True

    def test_a_target_nothing_reaches_was_still_pulsed(self, monkeypatch) -> None:
        issued = self._cli(monkeypatch, {})
        probe = self._probe()
        info = probe.identify()
        assert not info.connected
        assert [m for m, _ in issued].count("HWRSTPULSE") == 2
        assert probe.target_was_reset is True

    def test_allow_reset_false_sends_no_pulse_and_reports_none(self, monkeypatch) -> None:
        issued = self._cli(monkeypatch, {})
        probe = self._probe()
        probe.identify(allow_reset=False)
        assert all(mode == "HOTPLUG" for mode, _ in issued)
        assert probe.target_was_reset is False

    def test_a_hotplug_connect_is_not_a_reset(self, monkeypatch) -> None:
        issued = self._cli(monkeypatch, {("HOTPLUG", 0): 0x450})
        probe = self._probe()
        probe.identify()
        assert issued == [("HOTPLUG", 0)]
        assert probe.target_was_reset is False

    def test_the_unmeasured_port_check_never_pulses(self, monkeypatch) -> None:
        """An unknown part reachable only by a resetting connect. The follow-up
        check of the other port promised a hotplug connect and reused the reset
        mode instead, pulsing the board a second time."""
        issued = self._cli(monkeypatch, {("HWRSTPULSE", 0): 0x999})
        probe = self._probe()
        assert probe.identify().connected
        assert [m for m, _ in issued].count("HWRSTPULSE") == 1
        assert issued[-1] == ("HOTPLUG", 1)
        assert probe.target_was_reset is True

    def _bench(self, monkeypatch):
        """The real Probe behind the real tools, with only the CLI faked."""
        monkeypatch.setattr(server, "_toolchain", lambda: SimpleNamespace(programmer="x"))
        monkeypatch.setattr(
            server, "_list_probes", lambda tc: [probe_mod.ProbeInfo(serial="AAA1")]
        )
        server._held.clear()

    def test_board_state_says_a_board_it_could_not_reach_was_reset(
        self, monkeypatch
    ) -> None:
        self._cli(monkeypatch, {})
        self._bench(monkeypatch)
        out = call("board_state")
        assert_contract(out)
        assert out["status"] == result.FAILED
        assert out["target_was_reset"] is True
        assert out["connect_mode"] is None, "nothing connected, so no mode did"

    def test_board_state_reports_the_n6_pulse(self, monkeypatch) -> None:
        self._cli(monkeypatch, {("HOTPLUG", 0): 0x486})
        self._bench(monkeypatch)
        out = call("board_state")
        assert_contract(out)
        assert out["status"] == result.UNVERIFIED
        assert out["target_was_reset"] is True

    def test_core_registers_checks_the_connect_and_reports_the_pulse(
        self, monkeypatch
    ) -> None:
        self._cli(monkeypatch, {})
        self._bench(monkeypatch)
        out = call("core_registers")
        assert_contract(out)
        assert out["status"] == result.FAILED
        assert out["target_was_reset"] is True
        assert "Most likely causes" in out["hint"]


class TestASeededAddressIsDisclosedWhereItIsUsed:
    """RT1 and RT8 from the phase 2 red-team.

    `flash_firmware` refuses an unmeasured base and names the provenance in the
    refusal, so a caller there cannot miss it. `run_firmware` ACTS on an unmeasured
    load address by design - a wrong one is undone by a reset and the console is a
    real observation - and it used to return `verified` with nothing in the payload
    saying the address came from ST's database rather than a board. An agent could
    then honestly write "confirmed this part's RAM layout" from a number nobody had
    measured, which is the precise harm the intent statement describes.

    These are also the only tests that drive run_firmware with a seeded entry at
    all. Before them, every test reaching run_firmware's body used the measured
    STM32N6, so the capability phase 2 exists to add had no coverage.
    """

    @pytest.fixture
    def seeded_bench(self, bench, monkeypatch):
        from stlink_mcp import quirks as quirks_mod

        seeded = quirks_mod.FamilyQuirks(
            name="STM32F4",
            access_port=0,
            ram_load_base=0x20000000,
            vector_table_addr=0x20000000,
            flash_base=0x08000000,
            notes="seeded from the vendor database, nothing measured",
            default_provenance=quirks_mod.VENDOR_DB,
            provenance={"access_port": quirks_mod.ASSUMED},
        )
        monkeypatch.setattr(probe_mod, "for_device_id", lambda did: seeded)
        monkeypatch.setattr(_FakeProbe, "device_id", 0x431)
        monkeypatch.setattr(_FakeProbe, "gdb_access_port", lambda self, info: 0)
        # The image has to be linked for THIS part's RAM, or run_firmware refuses
        # it before any load (RT1, 2026-09-03).
        from elfgen import minimal_elf32
        bench.elf.write_bytes(minimal_elf32(seeded.vector_table_addr, 0x1000))
        return bench

    def test_a_seeded_part_can_actually_run_firmware(self, seeded_bench) -> None:
        """The end-to-end capability, which had no test. A seeded entry must be
        usable, not merely parseable."""
        seeded_bench.console_text = "hello from a part nobody measured\n"
        out = call("run_firmware", elf_path=str(seeded_bench.elf))
        assert out["ok"] is True
        assert out["load_address"] == "0x20000000"

    def test_the_result_says_the_load_address_was_not_measured(self, seeded_bench) -> None:
        seeded_bench.console_text = "alive\n"
        out = call("run_firmware", elf_path=str(seeded_bench.elf))
        assert out["load_address_provenance"] == "vendor-db"
        assert "not\nmeasured" in out["load_address_note"].replace(" ", "\n") or \
            "not measured" in out["load_address_note"]

    def test_it_does_not_invite_the_agent_to_record_a_confirmed_layout(
        self, seeded_bench
    ) -> None:
        """The note has to separate two things a successful run does NOT
        distinguish: that the image started, and that the address is right for
        this part in general."""
        seeded_bench.console_text = "alive\n"
        note = call("run_firmware", elf_path=str(seeded_bench.elf))["load_address_note"]
        assert "confirmed layout" in note or "confirmed" in note

    def test_an_unverified_run_is_not_called_verified_in_the_note(self, seeded_bench) -> None:
        """Measured 2026-10-01 on an STM32F407 with no console: the run was
        unverified and its note said "The run itself IS verified"."""
        seeded_bench.console_text = ""
        out = call("run_firmware", elf_path=str(seeded_bench.elf))
        assert out["status"] == result.UNVERIFIED
        assert "IS verified" not in out["load_address_note"]
        assert "not a measurement" in out["load_address_note"]

    def test_a_measured_part_carries_no_such_note(self, bench) -> None:
        """The fake board is the measured STM32N6, so the caveat must be absent
        rather than always-on noise."""
        bench.console_text = "alive\n"
        out = call("run_firmware", elf_path=str(bench.elf))
        assert out["load_address_provenance"] == "measured"
        assert "load_address_note" not in out


class TestARefusedDeviceIsNotTheSameAsAnUnknownOne:
    """RT3. The four ambiguous device IDs are refused deliberately, and the reason
    naming the candidate addresses was computed and then reduced to a count by the
    first caller. At runtime they looked identical to a part ST has never heard
    of."""

    def test_board_state_reports_why_the_database_entry_was_refused(
        self, bench, monkeypatch
    ) -> None:
        from stlink_mcp import quirks as quirks_mod

        monkeypatch.setattr(_FakeProbe, "device_id", 0x42B)
        monkeypatch.setattr(probe_mod, "for_device_id", lambda did: quirks_mod.DEFAULT)
        monkeypatch.setattr(
            quirks_mod, "_VENDOR_REFUSALS",
            {0x42B: "the database names 0x20010000, 0x30004000 as SRAM"},
        )
        family = call("board_state")["family"]
        assert "0x20010000" in family["vendor_database_refused"]
        assert "board file" in family["how_to_proceed"]

    def test_a_genuinely_unknown_part_gets_no_such_claim(self, bench, monkeypatch) -> None:
        from stlink_mcp import quirks as quirks_mod

        monkeypatch.setattr(_FakeProbe, "device_id", 0xDEAD)
        monkeypatch.setattr(probe_mod, "for_device_id", lambda did: quirks_mod.DEFAULT)
        monkeypatch.setattr(quirks_mod, "_VENDOR_REFUSALS", {})
        family = call("board_state")["family"]
        assert "vendor_database_refused" not in family


class TestFaultStatusReportsWhatsActuallyThere:
    """Phase 5: read and decode the Cortex-M fault status registers.

    The tool reads the SCB fault registers without halting or resetting the
    target, decodes every set bit, and reports a plain-English diagnosis. When
    a fault is active it also gives the agent a next step to find where the
    fault happened.
    """

    def test_clean_registers_report_no_fault(self, bench) -> None:
        out = call("read_fault_status")
        assert_contract(out)
        assert out["any_fault_active"] is False
        assert "no fault" in out["diagnosis"]
        assert "next_step" not in out

    def test_active_fault_includes_next_step(self, bench, monkeypatch) -> None:
        from stlink_mcp import faults as fm

        def read_with_fault(self, address, length_bytes):
            words = {address + i * 4: 0 for i in range(length_bytes // 4)}
            if address == fm.FAULT_REGION_BASE:
                words[fm.CFSR] = (1 << 9) | (1 << 15)  # PRECISERR + BFARVALID
                words[fm.HFSR] = 1 << 30  # FORCED
                words[fm.BFAR] = 0x40001234
            return words

        monkeypatch.setattr(_FakeProbe, "read_memory", read_with_fault)
        out = call("read_fault_status")
        assert_contract(out)
        assert out["any_fault_active"] is True
        assert out["bus_fault_address"] == "0x40001234"
        assert "0x40001234" in out["diagnosis"]
        assert "escalated" in out["diagnosis"]
        assert "locate_fault" in out["next_step"]

    def test_does_not_reset_the_target(self, bench) -> None:
        out = call("read_fault_status")
        assert out["target_was_reset"] is False

    def test_raw_registers_are_reported(self, bench) -> None:
        out = call("read_fault_status")
        raw = out["raw_registers"]
        assert {"CFSR", "HFSR", "DFSR", "AFSR"} <= set(raw)
        # BFAR and MMFAR only when CFSR marks them valid, which this clean read does not.
        assert "BFAR" not in raw and "MMFAR" not in raw

    @pytest.mark.parametrize("cpu", ["Cortex-M0", "Cortex-M0+", "Cortex-M23"])
    def test_a_core_without_the_registers_is_refused_before_any_read(
        self, bench, monkeypatch, cpu: str
    ) -> None:
        """Armv6-M and Armv8-M Baseline take every fault as a HardFault and record
        nothing in this region. Reading it would decode as "no fault active" for
        a core sitting in its HardFault handler."""
        def must_not_read(self, address, length_bytes):
            raise AssertionError("the region was read on a core that lacks it")

        monkeypatch.setattr(_FakeProbe, "cpu", cpu)
        monkeypatch.setattr(_FakeProbe, "read_memory", must_not_read)
        out = call("read_fault_status")
        assert_contract(out)
        assert out["status"] == result.FAILED
        assert "no configurable fault status registers" in out["error"]
        assert "XPSR" in out["hint"]

    def test_a_partial_read_is_refused_rather_than_decoded_as_clean(
        self, bench, monkeypatch
    ) -> None:
        """decode() treats an absent word as zero, which is "no fault bit set"."""
        from stlink_mcp import faults as fm

        monkeypatch.setattr(
            _FakeProbe, "read_memory",
            lambda self, address, n: {fm.DFSR: 0, fm.MMFAR: 0, fm.BFAR: 0, fm.AFSR: 0},
        )
        out = call("read_fault_status")
        assert_contract(out)
        assert out["status"] == result.FAILED
        assert "CFSR, HFSR" in out["error"]
        assert "any_fault_active" not in out

    def test_a_clean_read_on_an_unrecognised_core_is_not_verified(
        self, bench, monkeypatch
    ) -> None:
        monkeypatch.setattr(_FakeProbe, "cpu", None)
        out = call("read_fault_status")
        assert_contract(out)
        assert out["status"] == result.UNVERIFIED
        assert "does not recognise" in out["unverified_because"]

    def test_set_bits_are_evidence_even_on_an_unrecognised_core(
        self, bench, monkeypatch
    ) -> None:
        from stlink_mcp import faults as fm

        def read_with_fault(self, address, length_bytes):
            words = {address + i * 4: 0 for i in range(length_bytes // 4)}
            words[fm.CFSR] = 1 << 17  # INVSTATE
            return words

        monkeypatch.setattr(_FakeProbe, "cpu", None)
        monkeypatch.setattr(_FakeProbe, "read_memory", read_with_fault)
        out = call("read_fault_status")
        assert out["status"] == result.VERIFIED
        assert out["any_fault_active"] is True

    def test_the_next_step_names_the_stack_the_frame_is_on(
        self, bench, monkeypatch
    ) -> None:
        """Reading the frame at "the SP" is wrong for any fault in RTOS thread
        code, whose frame is on PSP while a handler's SP is MSP. The step now
        sends the agent to locate_fault, which reads EXC_RETURN, rather than
        through the procedure by hand."""
        from stlink_mcp import faults as fm

        def read_with_fault(self, address, length_bytes):
            words = {address + i * 4: 0 for i in range(length_bytes // 4)}
            words[fm.HFSR] = 1 << 30
            return words

        monkeypatch.setattr(_FakeProbe, "read_memory", read_with_fault)
        step = call("read_fault_status")["next_step"]
        assert "locate_fault" in step and "MSP or PSP" in step and "the SP" not in step

    def test_the_security_bank_caveat_appears_only_where_it_can_apply(
        self, bench, monkeypatch
    ) -> None:
        assert "security_state_note" in call("read_fault_status")  # Cortex-M55
        monkeypatch.setattr(_FakeProbe, "cpu", "Cortex-M7")
        assert "security_state_note" not in call("read_fault_status")

    def test_debug_events_since_cleared_reported_when_present(
        self, bench, monkeypatch
    ) -> None:
        from stlink_mcp import faults as fm

        def read_with_halt(self, address, length_bytes):
            words = {address + i * 4: 0 for i in range(length_bytes // 4)}
            if address == fm.FAULT_REGION_BASE:
                words[fm.DFSR] = 1 << 1  # BKPT
            return words

        monkeypatch.setattr(_FakeProbe, "read_memory", read_with_halt)
        out = call("read_fault_status")
        assert_contract(out)
        assert out["any_fault_active"] is False
        reasons = out["debug_events_since_cleared"]
        assert any(r["bit"] == "BKPT" for r in reasons)


class TestFlashIsReadBackBeforeItIsVerified:
    """The one tool whose write persists used to verify on the programmer's own
    "File download complete", the vendor banner the README's first example shows
    cannot be trusted. It now reads the range back and compares it."""

    @pytest.fixture(autouse=True)
    def _reset_readback(self):
        yield
        _FakeProbe.readback = None

    def test_an_identical_readback_is_what_verifies(self, bench) -> None:
        bench.bin.write_bytes(bytes(range(16)))
        out = call("flash_firmware", image_path=str(bench.bin), address="0x70000000")
        assert_contract(out)
        assert out["status"] == result.VERIFIED
        assert "read back 16 bytes" in out["verified_by"]

    def test_a_part_that_boots_only_signed_images_is_told_to_sign(self, bench) -> None:
        """The bench is an STM32N6, whose boot ROM starts only a signed image. Its
        next step said only to set the boot switches (found 2026-10-03, writing
        the N6's external flash for the first time)."""
        bench.bin.write_bytes(bytes(range(16)))
        out = call("flash_firmware", image_path=str(bench.bin), address="0x70000000")
        assert out["status"] == result.VERIFIED and out["signing_required"] is True
        assert "sign it" in out["next_step"]

    def test_flash_that_differs_from_the_image_fails_and_says_where(self, bench) -> None:
        bench.bin.write_bytes(bytes(range(16)))
        flash = bytearray(range(16)); flash[5] = 0xFF
        _FakeProbe.readback = bytes(flash)
        out = call("flash_firmware", image_path=str(bench.bin), address="0x70000000")
        assert_contract(out)
        assert out["status"] == result.FAILED
        assert out["first_difference"] == "0x70000005"
        assert out["differing_bytes"] == 1
        assert "0x05" in out["error"] and "0xFF" in out["error"]

    def test_a_readback_that_fails_is_not_success(self, bench) -> None:
        _FakeProbe.readback = "Error: failed to read the requested memory content"
        out = call("flash_firmware", image_path=str(bench.bin), address="0x70000000")
        assert_contract(out)
        assert out["status"] == result.UNVERIFIED
        assert "failed to read" in out["unverified_because"]


class TestTheEraseBeyondTheImageIsSaid:
    """16 KB written at 0x70000000 on an STM32N6 erased 64 KB, and the result
    said only that the 16 KB read back identical (2026-10-03). The flash here
    erases 64 KB sectors, as the N6's external flash did."""

    BASE, SECTOR = 0x70000000, 0x10000

    def _flash(self, monkeypatch, content: bytes) -> bytearray:
        model = bytearray(content) + bytearray(b"\xff" * (0x40000 - len(content)))

        def program(probe, image, address, loader):
            data = Path(image).read_bytes()
            offset = address - self.BASE
            end = -(-(offset + len(data)) // self.SECTOR) * self.SECTOR
            model[offset:end] = b"\xff" * (end - offset)
            model[offset:offset + len(data)] = data
            return "Erasing memory corresponding to segment 0:\nErasing external memory sector 0\nFile download complete\n"

        def read_back(probe, address, size, loader):
            offset = address - self.BASE
            return bytes(model[offset:offset + size])

        monkeypatch.setattr(_FakeProbe, "program_flash", program)
        monkeypatch.setattr(_FakeProbe, "read_back", read_back)
        return model

    def test_data_after_the_image_in_its_sector_is_reported_erased(self, bench, monkeypatch) -> None:
        self._flash(monkeypatch, b"\x11" * 0x20000)                # 128 KB of data, no 0xFF in it
        bench.bin.write_bytes(b"\x5a" * 0x4000)
        out = call("flash_firmware", image_path=str(bench.bin), address="0x70000000")
        assert_contract(out)
        assert out["status"] == result.VERIFIED
        erased = out["erased_beyond_image"]
        assert (erased["from"], erased["to"], erased["bytes"]) == ("0x70004000", "0x70010000", 0xC000)
        assert "whole sectors" in erased["note"]
        assert out["programmer_erase"] == ["Erasing memory corresponding to segment 0:", "Erasing external memory sector 0"]

    def test_blank_flash_after_the_image_cannot_show_the_erase(self, bench, monkeypatch) -> None:
        self._flash(monkeypatch, b"")
        bench.bin.write_bytes(b"\x5a" * 0x4000)
        out = call("flash_firmware", image_path=str(bench.bin), address="0x70000000")
        assert out["erased_beyond_image"]["bytes"] == 0 and "already blank" in out["erased_beyond_image"]["note"]

    def test_an_image_that_fills_its_sector_takes_nothing_else(self, bench, monkeypatch) -> None:
        self._flash(monkeypatch, b"\x11" * 0x20000)
        bench.bin.write_bytes(b"\x5a" * self.SECTOR)
        out = call("flash_firmware", image_path=str(bench.bin), address="0x70000000")
        erased = out["erased_beyond_image"]
        assert erased["bytes"] == 0 and "took nothing after the image" in erased["note"]


class TestReadBackUsesTheProgrammersUpload:
    def _probe(self, monkeypatch, write: bytes | None):
        from stlink_mcp import probe as pm
        from stlink_mcp.toolchain import CommandResult
        seen = {}

        def fake_run(argv, timeout=90):
            seen["argv"] = argv
            if write is not None:
                Path(argv[-1]).write_bytes(write)
            return CommandResult(argv, 0, "Data read successfully\n")

        monkeypatch.setattr(pm, "run_command", fake_run)
        return pm.Probe(toolchain=SimpleNamespace(programmer="x"), serial="AAA"), seen

    def test_it_returns_what_the_upload_wrote(self, monkeypatch) -> None:
        p, seen = self._probe(monkeypatch, b"\x01\x02\x03\x04")
        assert p.read_back(0x08000000, 4) == b"\x01\x02\x03\x04"
        assert seen["argv"][-4:-1] == ["-u", "0x08000000", "4"]

    def test_a_short_upload_is_an_error_not_a_shorter_image(self, monkeypatch) -> None:
        from stlink_mcp import probe as pm
        p, _ = self._probe(monkeypatch, b"\x01\x02")
        with pytest.raises(pm.ProbeError, match="returned 2"):
            p.read_back(0x08000000, 4)

    def test_no_file_at_all_is_an_error(self, monkeypatch) -> None:
        from stlink_mcp import probe as pm
        p, _ = self._probe(monkeypatch, None)
        with pytest.raises(pm.ProbeError, match="returned 0"):
            p.read_back(0x08000000, 4)


class TestTheInterfaceReachesTheProbeAndTheServer:
    """N8, at the tool level: the parameter is passed on, and the result says it."""

    def test_board_state_connects_over_the_interface_asked_for(self, bench, monkeypatch) -> None:
        made: list[str] = []
        real_init = _FakeProbe.__init__

        def recording_init(self, *a, **k):
            real_init(self, *a, **k)
            made.append(self.interface)

        monkeypatch.setattr(_FakeProbe, "__init__", recording_init)
        out = call("board_state", interface="jtag")
        assert_contract(out)
        assert made == ["jtag"]
        assert out["interface"] == "jtag"

    def test_run_firmware_starts_its_server_for_that_interface(self, bench, monkeypatch) -> None:
        asked: dict = {}
        monkeypatch.setattr(server, "acquire_server", lambda *a, **k: asked.update(k))
        bench.console_text = "tick 0\n"
        out = call("run_firmware", elf_path=str(bench.elf), interface="jtag")
        assert_contract(out)
        assert asked.get("interface") == "jtag"
        assert out["interface"] == "jtag"


class TestAPlaceInTheSourceHasOneShape:
    """locate_fault gave "location": "file:line" where run_until and
    core_registers give file and line apart (interface review, 2026-10-03). The
    addr2line output is what it printed for tests/fixtures/elf/h7_vars_dwarf5.elf."""

    @pytest.mark.parametrize("printed, expected", [
        ("writer\nimages/debug_target.c:39\n", ("writer", "images/debug_target.c", 39)),
        ("delay\nimages/debug_target.c:48 (discriminator 3)\n", ("delay", "images/debug_target.c", 48)),
        ("??\n??:0\n", (None, None, None)),
    ])
    def test_function_file_line_address(self, monkeypatch, printed, expected) -> None:
        monkeypatch.setattr(server, "run_command", lambda argv, timeout: SimpleNamespace(stdout=printed))
        place = server._symbol(SimpleNamespace(addr2line=Path("addr2line")), Path("x.elf"), 0x24000042)
        assert (place["function"], place["file"], place["line"]) == expected
        assert list(place) == ["function", "file", "line", "address"] and place["address"] == "0x24000042"


class TestLocateFaultFollowsTheFrame:
    """locate_fault does in one call what read_fault_status's next step says to
    do by hand, which was followed on an H7 and an N6 on 2026-10-01: XPSR says a
    handler is active, EXC_RETURN in LR names the stack, word 6 is the PC."""

    MSP, PSP = 0x20001000, 0x20002000
    UDF = 0x0800013E

    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch):
        monkeypatch.setattr(server, "_symbol", lambda tc, elf, addr: {"address": hex(addr)})
        yield
        _FakeProbe.memory = {}
        _FakeProbe.registers = {"PC": 0x34180D3C}
        _FakeProbe.cpu = "Cortex-M55"

    def _fault(self, lr: int, xpsr: int = 0x01000003, at: int | None = None,
               stacked_xpsr: int = 0x61000000) -> None:
        _FakeProbe.registers = {"PC": 0x08000170, "LR": lr, "XPSR": xpsr,
                                "MSP": self.MSP, "PSP": self.PSP}
        frame = at if at is not None else (self.PSP if lr & 4 else self.MSP)
        words = [1, 2, 3, 4, 12, 0x080000D3, self.UDF, stacked_xpsr]
        _FakeProbe.memory = {frame + 4 * i: w for i, w in enumerate(words)}

    def test_a_frame_on_msp_gives_the_faulting_pc(self, bench) -> None:
        self._fault(0xFFFFFFF9)
        out = call("locate_fault")
        assert_contract(out)
        assert out["status"] == result.VERIFIED
        assert (out["stack"], out["faulting_pc"]) == ("MSP", "0x0800013E")
        assert out["exception"] == {"number": 3, "name": "HardFault"}
        assert out["halted_during_call"] is True

    def test_a_thread_on_psp_is_read_from_psp(self, bench) -> None:
        self._fault(0xFFFFFFFD)
        out = call("locate_fault")
        assert (out["stack"], out["frame_address"], out["faulting_pc"]) == ("PSP", "0x20002000", "0x0800013E")

    def test_an_fp_frame_is_marked_and_still_read_at_word_6(self, bench) -> None:
        self._fault(0xFFFFFFE9)
        out = call("locate_fault")
        assert out["fp_frame"] is True and out["faulting_pc"] == "0x0800013E"

    def test_armv8m_additional_state_moves_the_frame_up_40_bytes(self, bench) -> None:
        self._fault(0xFFFFFFD9, at=self.MSP + 40)
        out = call("locate_fault")
        assert out["status"] == result.VERIFIED
        assert out["frame_address"] == "0x20001028"
        assert any("Secure" in n for n in out["notes"])

    def test_the_same_lr_on_an_armv7m_core_has_no_additional_state(self, bench) -> None:
        _FakeProbe.cpu = "Cortex-M7"
        self._fault(0xFFFFFFD9)
        out = call("locate_fault")
        assert out["frame_address"] == "0x20001000"
        assert "notes" not in out

    def test_thread_mode_has_no_frame_to_read(self, bench) -> None:
        self._fault(0xFFFFFFF9, xpsr=0x61000000)
        out = call("locate_fault")
        assert_contract(out)
        assert out["status"] == result.FAILED and "thread mode" in out["error"]

    def test_an_overwritten_lr_is_refused_not_guessed(self, bench) -> None:
        self._fault(0x08000123)
        out = call("locate_fault")
        assert out["status"] == result.FAILED and "not an EXC_RETURN" in out["error"]

    def test_words_without_the_thumb_bit_are_not_a_frame(self, bench) -> None:
        """What the H7's own HardFault handler produced on 2026-10-01: it pushed
        registers first, so the words at MSP were not the frame."""
        self._fault(0xFFFFFFF9, stacked_xpsr=0x2407FFB4)
        out = call("locate_fault")
        assert_contract(out)
        assert out["status"] == result.UNVERIFIED
        assert "Thumb bit" in out["unverified_because"]

    def test_a_pc_outside_the_elf_is_called_out(self, bench) -> None:
        self._fault(0xFFFFFFF9)
        out = call("locate_fault", elf_path=str(bench.elf))
        assert out["status"] == result.UNVERIFIED
        assert "outside every segment" in out["unverified_because"]
        assert out["faulting_function"] == {"address": hex(self.UDF)}

    def test_an_imprecise_bus_error_warns_that_the_pc_is_late(self, bench) -> None:
        from stlink_mcp import faults
        self._fault(0xFFFFFFF9)
        _FakeProbe.memory[faults.CFSR] = 1 << (8 + 2)  # BFSR.IMPRECISERR
        out = call("locate_fault")
        assert any("imprecise" in n for n in out["notes"])

    def test_a_reset_to_reach_the_core_is_not_a_location(self, bench, monkeypatch) -> None:
        self._fault(0xFFFFFFF9)
        real_identify = _FakeProbe.identify

        def resetting(self, allow_reset=True):
            self.target_was_reset = True
            return real_identify(self, allow_reset)

        monkeypatch.setattr(_FakeProbe, "identify", resetting)
        out = call("locate_fault")
        assert out["status"] == result.FAILED and "restarted" in out["error"]


class TestTheLastSettledPortIsTriedFirst:
    """On an STM32N6 AP0 answers identity and AP1 is the port that works, so a
    scan from AP0 costs an extra connect, 1.1 s there, on every call."""

    def _probe(self, monkeypatch, try_first):
        from stlink_mcp import probe as pm
        from stlink_mcp.toolchain import CommandResult
        tried: list[int] = []

        def fake_run(argv, timeout=90):
            tried.append(next(int(a.split("=")[1]) for a in argv if a.startswith("ap=")))
            return CommandResult(argv, 0, "Device ID   : 0x486\nDevice CPU  : Cortex-M55\n")

        monkeypatch.setattr(pm, "run_command", fake_run)
        p = pm.Probe(toolchain=SimpleNamespace(programmer="x"), serial="AAAA0001", try_first=try_first)
        return p, tried

    def test_with_no_hint_the_scan_starts_at_ap0_and_settles_on_ap1(self, monkeypatch) -> None:
        p, tried = self._probe(monkeypatch, None)
        p.identify()
        assert tried == [0, 1] and p.access_port == 1

    def test_with_ap1_first_one_connect_identifies(self, monkeypatch) -> None:
        p, tried = self._probe(monkeypatch, 1)
        p.identify()
        assert tried == [1] and p.access_port == 1

    def test_a_wrong_hint_still_settles_on_the_familys_port(self, monkeypatch) -> None:
        p, tried = self._probe(monkeypatch, 0)
        p.identify()
        assert p.access_port == 1


class TestTheConsoleIsFoundByTheProbesSerial:
    """On Linux udev names each serial port after its USB serial number, which
    for an ST-LINK is the serial the programmer reports, so a named probe's
    console needs no listing (checked on two STLINK-V3s, 2026-10-01)."""

    SERIAL = "0054003D3137511139383538"

    def _by_id(self, tmp_path, monkeypatch, names):
        d = tmp_path / "by-id"
        d.mkdir()
        for name in names:
            # Only the name is read. A symlink needs a privilege Windows does not
            # give an ordinary process (WinError 1314 on Windows 11, 2026-10-02).
            (d / name).touch()
        monkeypatch.setattr(server, "_BY_ID_DIR", d)
        return d

    def test_the_port_named_after_the_probe_is_the_console(self, bench, tmp_path, monkeypatch) -> None:
        d = self._by_id(tmp_path, monkeypatch, [
            f"usb-STMicroelectronics_STLINK-V3_{self.SERIAL}-if02",
            "usb-STMicroelectronics_STLINK-V3_003C002C3335510535383531-if01",
        ])
        def no_listing(tc):
            raise AssertionError("a named probe's console must not need a listing")
        monkeypatch.setattr(server, "_list_probes", no_listing)
        bench.console_text = "tick 0\n"
        out = call("capture_console", probe_serial=self.SERIAL)
        assert_contract(out)
        assert out["console_port"] == str(d / f"usb-STMicroelectronics_STLINK-V3_{self.SERIAL}-if02")
        assert out["console_port_source"] == "udev serial name"

    def test_a_probe_with_no_port_has_no_console(self, bench, tmp_path, monkeypatch) -> None:
        self._by_id(tmp_path, monkeypatch, ["usb-STMicroelectronics_STLINK-V3_003C002C3335510535383531-if01"])
        out = call("capture_console", probe_serial="53FF75065177485736340687")
        assert out["status"] == result.FAILED
        assert "No serial console was found" in out["error"]

    def test_two_ports_for_one_probe_are_not_chosen_between(self, bench, tmp_path, monkeypatch) -> None:
        self._by_id(tmp_path, monkeypatch, [
            f"usb-STMicroelectronics_STLINK-V3_{self.SERIAL}-if02",
            f"usb-STMicroelectronics_STLINK-V3_{self.SERIAL}-if05",
        ])
        out = call("capture_console", probe_serial=self.SERIAL)
        assert out["status"] == result.FAILED
        assert "Several serial ports" in out["console_note"]

    def test_a_pair_setting_still_wins(self, bench, tmp_path, monkeypatch) -> None:
        self._by_id(tmp_path, monkeypatch, [f"usb-STMicroelectronics_STLINK-V3_{self.SERIAL}-if02"])
        monkeypatch.setenv("STLINK_MCP_CONSOLE", f"{self.SERIAL}=/dev/ttyUSB3")
        bench.console_text = "tick 0\n"
        out = call("capture_console", probe_serial=self.SERIAL)
        assert out["console_port"] == "/dev/ttyUSB3" and out["console_port_source"] == "STLINK_MCP_CONSOLE"


class TestAConsoleThatIsNotTextSaysWhy:
    """Measured 2026-10-01: a 57600-baud image read at 115200 verified on
    output nobody could read."""

    def test_garbled_output_carries_a_baud_note(self, bench) -> None:
        bench.console_text = "\ufffd\ufffd\u679e\ufffd\ufffd\ufffd\u6606\ufffd\ufffd\u6618\ufffd\ufffd\ufffd\ufffd\u679e\ufffd\ufffd"
        out = call("run_firmware", elf_path=str(bench.elf))
        assert out["status"] == result.VERIFIED, "bytes did arrive after the start"
        assert "baud" in out["console_garbled_note"]

    def test_ordinary_text_carries_no_note(self, bench) -> None:
        bench.console_text = "stlink-mcp hw check ram tick 0\r\nstlink-mcp hw check ram tick 1\r\n"
        out = call("run_firmware", elf_path=str(bench.elf))
        assert "console_garbled_note" not in out

    def test_capture_console_says_it_too(self, bench) -> None:
        bench.console_text = "\ufffd" * 40
        out = call("capture_console")
        assert "console_garbled_note" in out


class TestThePortWarningReachesTheToolThatFails:
    """Red-team RT7 (2026-09-03): on a part whose access port nobody measured,
    board_state warned that the port was a guess, and core_registers then
    failed to halt the core on that port without a word about it."""

    def test_a_failed_halt_on_an_unmeasured_port_says_so(self, monkeypatch) -> None:
        from stlink_mcp import probe as pm
        from stlink_mcp.toolchain import CommandResult

        def fake_run(argv, timeout=90):
            if "-coreReg" in argv:
                return CommandResult(argv, 0, "Device ID   : 0x431\nError: Failed to halt target\n")
            return CommandResult(argv, 0, "Device ID   : 0x431\nDevice CPU  : Cortex-M4\n")

        monkeypatch.setattr(pm, "run_command", fake_run)
        monkeypatch.setattr(server, "_toolchain", lambda: SimpleNamespace(programmer="x"))
        monkeypatch.setattr(server, "_SETTLED_PORTS", {})
        out = call("core_registers", probe_serial="AAAA0001")
        assert_contract(out)
        assert out["status"] == result.FAILED
        assert "nobody measured" in out["access_port_warning"]
        assert "AP1 also answered" in out["access_port_warning"]

    def test_a_measured_port_carries_no_warning(self, bench) -> None:
        out = call("read_memory", probe_serial="AAAA0001")
        assert "access_port_warning" not in out

    def test_the_warning_survives_a_remembered_port_on_the_other_side(self, monkeypatch) -> None:
        """Found when a remembered AP1 from another test reached this one: the
        scan answered on AP1, settled on the assumed AP0 by the other branch,
        and that branch did not warn."""
        from stlink_mcp import probe as pm
        from stlink_mcp.toolchain import CommandResult
        monkeypatch.setattr(pm, "run_command", lambda argv, timeout=90: CommandResult(
            argv, 0, "Device ID   : 0x431\nDevice CPU  : Cortex-M4\n"))
        p = pm.Probe(toolchain=SimpleNamespace(programmer="x"), serial="AAAA0001", try_first=1)
        p.identify()
        assert p.access_port == 0 and "nobody measured" in p.unmeasured_port_warning


class TestAnEmptyListingSaysWhatItCanShow:
    """Red-team RT10 (2026-09-03): errors and no entries read as "verified, 0
    probes", so the agent could not tell unplugged from blocked."""

    def test_errors_with_no_probe_are_not_a_verified_zero(self, bench, monkeypatch) -> None:
        errs = ["ST-LINK error (DEV_USB_COMM_ERR)"]
        monkeypatch.setattr(server, "list_probes_with_unreadable", lambda tc: ([], 0, errs))
        out = call("list_probes")
        assert_contract(out)
        assert out["status"] == result.UNVERIFIED
        assert out["errors"] == errs and "udev" in out["hint"]

    def test_an_empty_bus_is_verified_and_points_below_the_tool(self, bench, monkeypatch) -> None:
        monkeypatch.setattr(server, "list_probes_with_unreadable", lambda tc: ([], 0, []))
        out = call("list_probes")
        assert out["status"] == result.VERIFIED and out["count"] == 0
        assert "lsusb" in out["hint"]


class TestEveryCallIsTracedToWhoAsked:
    """N9: nothing assumes a single implicit caller. The caller is always the
    local user today; it is recorded so identity and policy can be added later
    without a redesign."""

    @staticmethod
    def _ctx(name: str, request: str):
        session = SimpleNamespace(client_params=SimpleNamespace(
            client_info=SimpleNamespace(name=name, version="1.0")))
        return SimpleNamespace(session=session, request_id=request)

    def test_a_held_probe_says_who_holds_it(self, bench, monkeypatch, tmp_path) -> None:
        by_id = tmp_path / "by-id"
        by_id.mkdir()
        (by_id / "usb-STMicroelectronics_STLINK-V3_AAAA0001-if02").touch()
        monkeypatch.setattr(server, "_BY_ID_DIR", by_id)
        bench.shares = False  # so read_memory refuses, and says who holds it
        bench.console_text = "tick 0\n"
        run = call("run_firmware", elf_path=str(bench.elf), probe_serial="AAAA0001", ctx=self._ctx("agent-a", "7"))
        assert run["status"] == result.VERIFIED, run
        refused = call("read_memory", probe_serial="AAAA0001", ctx=self._ctx("agent-b", "3"))
        assert refused["status"] == result.FAILED
        assert refused["held_by"].startswith("agent-a 1.0, connection ")
        assert refused["held_by"].endswith(", request 7")
        released = call("release_probe", probe_serial="AAAA0001", ctx=self._ctx("agent-b", "4"))
        assert "agent-a 1.0" in released["held_by"]["AAAA0001"]

    def test_each_call_leaves_one_trace_line(self, bench, capsys) -> None:
        call("read_memory", probe_serial="AAAA0001", ctx=self._ctx("agent-a", "9"))
        lines = [l for l in capsys.readouterr().err.splitlines() if "read_memory(" in l]
        assert len(lines) == 1
        assert "agent-a 1.0" in lines[0] and "request 9" in lines[0]
        assert "probe_serial=AAAA0001" in lines[0] and lines[0].endswith("-> verified")

    def test_a_call_made_in_process_is_traced_as_such(self, bench, capsys) -> None:
        call("read_memory", probe_serial="AAAA0001")
        assert "[in-process, connection 0, request -]" in capsys.readouterr().err

    def test_one_connection_keeps_its_number_across_requests(self) -> None:
        """The SDK builds a new session object for every request and keeps the
        client's initialize parameters once per connection (measured with a
        toy server over stdio, 2026-10-01). Keying on the session numbered each
        call as a new connection."""
        params = SimpleNamespace(client_info=SimpleNamespace(name="agent", version="1"))
        calls = [SimpleNamespace(session=SimpleNamespace(client_params=params), request_id=str(i)) for i in range(3)]
        assert len({server._caller_from(c).connection for c in calls}) == 1

    def test_two_connections_are_told_apart(self) -> None:
        one, other = self._ctx("x", "1"), self._ctx("x", "1")  # both connections stay open
        first = server._caller_from(one)
        second = server._caller_from(other)
        assert first.connection != second.connection
        assert server._caller_from(other).connection == second.connection


class TestOneProgrammerCallWhereNothingCanDiffer:
    """read_memory and read_fault_status connect and read in one programmer call
    when the probe's remembered port is the family's measured port, which halves
    an N6 call (a connect costs 1.1 s there, measured 2026-10-01)."""

    BANNER_N6 = "ST-LINK SN  : AAAA0001\nDevice ID   : 0x486\nDevice CPU  : Cortex-M55\n"
    BANNER_UNKNOWN = "ST-LINK SN  : AAAA0001\nDevice ID   : 0x431\nDevice CPU  : Cortex-M4\n"

    def _probe(self, monkeypatch, banner: str, try_first):
        from stlink_mcp import probe as pm
        from stlink_mcp.toolchain import CommandResult
        calls: list[list[str]] = []

        def fake_run(argv, timeout=90):
            calls.append(argv)
            body = banner + ("0x341B0000 : C0FFEE03\n" if "-r32" in argv else "")
            return CommandResult(argv, 0, body)

        monkeypatch.setattr(pm, "run_command", fake_run)
        return pm.Probe(toolchain=SimpleNamespace(programmer="x"), serial="AAAA0001", try_first=try_first), calls

    def test_a_remembered_measured_port_takes_one_call(self, monkeypatch) -> None:
        p, calls = self._probe(monkeypatch, self.BANNER_N6, 1)
        info, words = p.identify_and_read(0x341B0000, 4)
        assert len(calls) == 1 and words == {0x341B0000: 0xC0FFEE03} and p.access_port == 1

    def test_no_remembered_port_is_not_tried(self, monkeypatch) -> None:
        p, calls = self._probe(monkeypatch, self.BANNER_N6, None)
        assert p.identify_and_read(0x341B0000, 4) is None and calls == []

    def test_a_port_that_is_not_the_familys_is_given_back(self, monkeypatch) -> None:
        p, calls = self._probe(monkeypatch, self.BANNER_N6, 0)  # the N6's port is AP1
        assert p.identify_and_read(0x341B0000, 4) is None

    def test_an_unmeasured_family_is_given_back(self, monkeypatch) -> None:
        p, calls = self._probe(monkeypatch, self.BANNER_UNKNOWN, 0)  # port 0 is only assumed
        assert p.identify_and_read(0x20000000, 4) is None

    def test_the_tool_uses_it(self, monkeypatch) -> None:
        p, calls = self._probe(monkeypatch, self.BANNER_N6, 1)
        monkeypatch.setattr(server, "_toolchain", lambda: SimpleNamespace(programmer="x"))
        monkeypatch.setattr(server, "_SETTLED_PORTS", {"AAAA0001": 1})
        out = call("read_memory", address="0x341B0000", length_bytes=4, probe_serial="AAAA0001")
        assert out["status"] == result.VERIFIED and out["words"] == {"0x341B0000": "0xC0FFEE03"}
        assert sum(1 for c in calls) == 1


class TestTheInstructionAtAPlace:
    """Agents decoded the faulting instruction by hand from read words in four of
    seven tasks of an evaluation (2026-10-04). The fixtures are what the
    toolchain's objdump printed for the evaluation's N6 crash image at its udf,
    and for its F407 hang image at a 32-bit bl."""

    @pytest.mark.parametrize("fixture, address, expected", [
        ("objdump_n6_udf.txt", 0x34180528, "udf #0"),
        ("objdump_f4_bl.txt", 0x20000082, "bl 20000058 <level3>"),
    ])
    def test_it_is_read_from_objdump(self, monkeypatch, fixture, address, expected) -> None:
        printed = (Path(__file__).parent / "fixtures" / fixture).read_text()
        monkeypatch.setattr(server, "run_command", lambda argv, timeout: SimpleNamespace(stdout=printed))
        assert server._instruction(SimpleNamespace(objdump=Path("objdump")), Path("x.elf"), address) == expected

    def test_an_address_objdump_did_not_print_gives_none(self, monkeypatch) -> None:
        printed = (Path(__file__).parent / "fixtures" / "objdump_n6_udf.txt").read_text()
        monkeypatch.setattr(server, "run_command", lambda argv, timeout: SimpleNamespace(stdout=printed))
        assert server._instruction(SimpleNamespace(objdump=Path("objdump")), Path("x.elf"), 0x34180530) is None
