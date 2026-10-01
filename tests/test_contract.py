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

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from stlink_mcp import probe as probe_mod
from stlink_mcp import result, server
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
    "reset_target": {},
    "read_fault_status": {},
    "locate_fault": {},
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

    # Class-level so a test can patch them; __init__ must not overwrite them.
    access_port_provenance: str | None = "measured"
    other_ports_that_answered: tuple[int, ...] = ()
    unmeasured_port_warning: str | None = None

    def __init__(self, *args, **kwargs) -> None:
        self.interface = kwargs.get("interface", "swd")
        self.connect_mode = "HOTPLUG"
        self.target_was_reset = False
        self.degraded_reason: str | None = None
        self.access_port = 1

    def identify(self, allow_reset: bool = True) -> probe_mod.TargetInfo:
        return probe_mod.TargetInfo(
            connected=True, device_id=self.device_id, cpu=self.cpu
        )

    def gdb_access_port(self, info: object) -> int:
        return 1

    # Class-level so a test can lay out memory and registers; zero elsewhere.
    memory: dict[int, int] = {}
    registers: dict[str, int] = {"PC": 0x34180D3C}

    def read_memory(self, address: int, length_bytes: int) -> dict[int, int]:
        n_words = max(1, length_bytes // 4)
        return {address + i * 4: self.memory.get(address + i * 4, 0) for i in range(n_words)}

    def core_registers(self) -> dict[str, int]:
        return dict(self.registers)

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

    monkeypatch.setattr(server, "_toolchain", lambda: object())
    monkeypatch.setattr(server, "_list_probes", _attached)
    monkeypatch.setattr(
        server, "list_probes_with_unreadable", lambda tc: (_attached(tc), 0)
    )
    monkeypatch.setattr(server, "Probe", _FakeProbe)
    monkeypatch.setattr(server, "acquire_server", lambda *a, **k: None)
    monkeypatch.setattr(
        server, "load_and_run", lambda *a, **k: (*state.markers, state.gdb_output)
    )
    monkeypatch.setattr(server.console_mod, "ConsoleCapture", _FakeCapture)
    # As on a machine without udev's port names, so a test takes the same path
    # here as in CI; the udev path has tests of its own.
    monkeypatch.setattr(server, "_BY_ID_DIR", tmp_path / "no-by-id")

    server._held.clear()
    server._gdb_ports.clear()
    yield state
    server._held.clear()
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
        monkeypatch.setattr(server, "list_probes_with_unreadable", lambda tc: ([], 0))
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
        monkeypatch.setattr(server, "server_is_held", lambda serial=None: True)
        r = call("run_firmware", elf_path=str(bench.elf))
        assert_contract(r)
        assert r["status"] == result.FAILED
        assert r["gdb_server_held"] is True
        assert "release_probe" in r["gdb_server_note"]

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
        core is."""
        bench.console_text = "up\n"
        r = call("run_firmware", elf_path=str(bench.elf))
        assert "stack_pointer" not in r and "program_counter" not in r
        assert r["image_initial_sp"] == "0x34200000"
        assert "not a reading of where the core is" in r["image_note"]


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
        assert out["output"] == "live\n"
        assert out["console_buffered_bytes"] == 1500
        assert len(out["console_buffered"]) == 1000
        assert "may be from before this capture" in out["console_buffered_note"]
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
        assert out["output"] == "boot ok\n"
        assert "device disconnected" in out["console_error"]


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

    UNMEASURED_DEVICE = 0x413  # STM32F4, not in FAMILIES: a plausible seeded part

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

      landed on the vector   NUCLEO-H743ZI, reset handler 0x0807FA98, reached
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
        assert out["core_released"] is True
        assert "resumed" in out["note"]

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
        assert out["halted_to_read_pc"] is True
        assert out["core_released"] is True


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
        assert out["core_released"] is True, (
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
        assert out["core_released"] is False
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
            assert out["core_released"] is True

    def test_nothing_is_released_when_nothing_was_halted(self, h7_bench, monkeypatch) -> None:
        released: list[int] = []
        monkeypatch.setattr(_FakeProbe, "reset_pc", None)
        monkeypatch.setattr(
            _FakeProbe, "release_core",
            lambda self, ap: released.append(ap) or "Core run\n",
        )
        out = call("reset_target")
        assert not released
        assert out["halted_to_read_pc"] is False
        assert out["core_released"] is False
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
        assert out["halted_to_read_registers"] is True
        assert "now halted" not in out["note"]
        assert "not claimed" in out["note"]


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
        """Measured on a NUCLEO-H743ZI: the CLI prints "Core run"."""
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
        monkeypatch.setattr(_FakeProbe, "device_id", 0x413)
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
        assert "core_registers" in out["next_step"]

    def test_does_not_reset_the_target(self, bench) -> None:
        out = call("read_fault_status")
        assert out["target_was_reset"] is False

    def test_raw_registers_are_reported(self, bench) -> None:
        out = call("read_fault_status")
        raw = out["raw_registers"]
        assert "CFSR" in raw
        assert "HFSR" in raw
        assert "BFAR" in raw

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
        code, whose frame is on PSP while a handler's SP is MSP."""
        from stlink_mcp import faults as fm

        def read_with_fault(self, address, length_bytes):
            words = {address + i * 4: 0 for i in range(length_bytes // 4)}
            words[fm.HFSR] = 1 << 30
            return words

        monkeypatch.setattr(_FakeProbe, "read_memory", read_with_fault)
        step = call("read_fault_status")["next_step"]
        assert "PSP" in step and "MSP" in step and "bit 2" in step

    def test_the_security_bank_caveat_appears_only_where_it_can_apply(
        self, bench, monkeypatch
    ) -> None:
        assert "security_state_note" in call("read_fault_status")  # Cortex-M55
        monkeypatch.setattr(_FakeProbe, "cpu", "Cortex-M7")
        assert "security_state_note" not in call("read_fault_status")

    def test_debug_halt_reason_reported_when_present(
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
        reasons = out["debug_halt_reason"]
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
        assert out["halted_to_read_registers"] is True

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
            (d / name).symlink_to("/dev/ttyACM9")
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
        assert out["port"] == str(d / f"usb-STMicroelectronics_STLINK-V3_{self.SERIAL}-if02")
        assert out["port_source"] == "udev serial name"

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
        assert out["port"] == "/dev/ttyUSB3" and out["port_source"] == "STLINK_MCP_CONSOLE"


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
