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

    device_id = 0x486  # STM32N6: the one family with a RAM-execution layout

    def __init__(self, *args, **kwargs) -> None:
        self.connect_mode = "HOTPLUG"
        self.target_was_reset = False
        self.degraded_reason: str | None = None
        self.access_port = 1

    def identify(self, allow_reset: bool = True) -> probe_mod.TargetInfo:
        return probe_mod.TargetInfo(connected=True, device_id=self.device_id)

    def gdb_access_port(self, info: object) -> int:
        return 1

    def read_memory(self, address: int, length_bytes: int) -> dict[int, int]:
        return {address: 0xDEADBEEF}

    def core_registers(self) -> dict[str, int]:
        return {"PC": 0x34180D3C}

    def program_flash(self, image: str, address: int, loader: object) -> str:
        return "File download complete\n"


@pytest.fixture
def bench(monkeypatch, tmp_path):
    """A whole fake bench: no CLI, no probe, no serial port, no board."""
    elf = tmp_path / "image.elf"
    elf.write_bytes(b"\x7fELF not really")

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

    monkeypatch.setattr(server, "_toolchain", lambda: object())
    monkeypatch.setattr(
        server,
        "_list_probes",
        lambda tc: [
            probe_mod.ProbeInfo(serial="AAA1", console_port=state.console_port)
        ],
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
