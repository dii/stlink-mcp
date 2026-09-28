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

    def read_memory(self, address: int, length_bytes: int) -> dict[int, int]:
        n_words = max(1, length_bytes // 4)
        return {address + i * 4: 0 for i in range(n_words)}

    def core_registers(self) -> dict[str, int]:
        return {"PC": 0x34180D3C}

    def program_flash(self, image: str, address: int, loader: object) -> str:
        return "File download complete\n"

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

    state = SimpleNamespace(
        elf=elf,
        markers=(0x34200000, 0x34180841, 2048),
        console_text="",
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

    def _attached(tc: object) -> list[probe_mod.ProbeInfo]:
        return [probe_mod.ProbeInfo(serial="AAA1", console_port=state.console_port)]

    monkeypatch.setattr(server, "_toolchain", lambda: object())
    monkeypatch.setattr(server, "_list_probes", _attached)
    monkeypatch.setattr(
        server, "list_probes_with_unreadable", lambda tc: (_attached(tc), 0)
    )
    monkeypatch.setattr(server, "Probe", _FakeProbe)
    monkeypatch.setattr(server, "acquire_server", lambda *a, **k: None)
    monkeypatch.setattr(server, "load_and_run", lambda *a, **k: (*state.markers, ""))
    monkeypatch.setattr(server.console_mod, "ConsoleCapture", _FakeCapture)

    server._held.clear()
    server._gdb_ports.clear()
    yield state
    server._held.clear()
    server._gdb_ports.clear()


class TestEveryToolKeepsTheContract:
    @pytest.mark.parametrize("name", sorted(TOOL_CALLS))
    def test_on_a_working_bench(self, name: str, bench) -> None:
        assert_contract(call(name, **({"elf_path": str(bench.elf)} if name == "run_firmware" else {})), name)

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
