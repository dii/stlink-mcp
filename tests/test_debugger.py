"""GDB/MI parsing and the stop helpers, against output captured from a
NUCLEO-H743ZI on 2026-10-02.

gdbmi_h7_transcript.txt is everything GDB printed in one session through
ST-LINK_gdbserver in attach mode, on the H7 running tools/hwcheck's h7_vars: the
memory map, the breakpoint and watchpoint units, a breakpoint at writer with its
call stack, a watchpoint on g_watched, and an interrupted run.
gdbserver_log16_h7_watch.txt is the server's own log at level 16 around three
watchpoint stops.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from stlink_mcp import debugger, gdbmi, runner

FIXTURES = Path(__file__).parent / "fixtures"
LINES = (FIXTURES / "gdbmi_h7_transcript.txt").read_text().splitlines()


def _records(prefix: str) -> list[gdbmi.Record]:
    return [gdbmi.parse(line) for line in LINES if line.startswith(prefix)]


class TestEveryLineParses:
    def test_the_whole_transcript(self) -> None:
        kinds = {r.kind for r in (gdbmi.parse(line) for line in LINES) if r is not None}
        assert {"^", "*", "=", "~"} <= kinds

    def test_a_breakpoint_stop(self) -> None:
        stop = next(r for r in _records("*stopped") if r.results.get("reason") == "breakpoint-hit")
        frame = stop.results["frame"]
        assert (frame["func"], frame["addr"], frame["line"]) == ("writer", "0x24000042", "39")
        assert frame["args"] == [{"name": "v", "value": "22"}]

    def test_a_watchpoint_stop_is_a_bare_trap(self) -> None:
        """The server's stop reply names no reason, so GDB cannot say watchpoint."""
        traps = [r for r in _records("*stopped") if r.results.get("signal-name") == "SIGTRAP"]
        assert traps and "reason" in traps[0].results and traps[0].results["reason"] == "signal-received"

    def test_strings_keep_their_escapes(self) -> None:
        console = [r.text for r in _records('~"') if r.text.startswith("0   y")]
        assert console == ["0   y  \t0x00000000 0x08000000 rw nocache \n"]


class _Replay:
    """Answers each command with the result GDB gave it in the transcript."""

    def __init__(self) -> None:
        self.answers: dict[str, list[gdbmi.Record]] = {}
        done = [gdbmi.parse(line) for line in LINES if line.startswith("^done")]
        for record in done:
            for key in ("memory", "stack", "variables", "register-names", "register-values", "asm_insns"):
                if key in record.results:
                    self.answers.setdefault(key, []).append(record)
        console, info_mem = [], None
        for line in LINES:
            record = gdbmi.parse(line)
            if record is None:
                continue
            if record.kind == "~":
                console.append(record.text)
            elif record.kind == "^":
                if any("Low Addr" in text for text in console):
                    info_mem = gdbmi.Record("^", "done", {}, "".join(console))
                console = []
        self.info_mem = info_mem

    def command(self, text: str, timeout: float = 30) -> gdbmi.Record:
        if text.startswith("-interpreter-exec console"):
            return self.info_mem
        if text.startswith("-data-read-memory-bytes"):
            address = int(text.split()[1], 16)
            for record in self.answers["memory"]:
                if int(record.results["memory"][0]["begin"], 16) == address:
                    return record
            raise gdbmi.MiError("not in the transcript")
        key = {"-stack-list-frames": "stack", "-stack-list-variables": "variables",
               "-data-list-register-names": "register-names", "-data-list-register-values": "register-values",
               "-data-disassemble": "asm_insns"}[text.split()[0]]
        return self.answers[key].pop(0)


class TestTheHelpersReadWhatGdbSaid:
    def test_the_h7s_units(self) -> None:
        units = debugger.comparators(_Replay())
        assert (units.breakpoint_unit_revision, units.breakpoints, units.watchpoints) == (1, 8, 4)
        assert units.covers(0x24000042)

    def test_a_revision_1_unit_covers_only_code(self) -> None:
        units = debugger.Comparators(0, 6, 4)
        assert units.covers(0x08000000) and not units.covers(0x20000042)

    def test_ram_and_flash_from_the_memory_map(self) -> None:
        replay = _Replay()
        assert debugger.writable(replay, 0x24000042) is True
        assert debugger.writable(replay, 0x08000000) is False

    def test_the_call_stack_ends_where_the_elf_does(self) -> None:
        frames, cut = debugger.frames(_Replay())
        assert [f["function"] for f in frames] == ["writer", "reset_handler"] and not cut
        assert frames[0]["arguments"] == {"v": "22"} and frames[0]["locals"] == {"s_count": "10"}

    def test_core_registers_by_name(self) -> None:
        regs = debugger.registers(_Replay())
        assert set(regs) == {"R0", "R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9", "R10",
                             "R11", "R12", "SP", "LR", "PC", "XPSR"}
        assert regs["PC"] == 0x24000042

    def test_the_call_before_lr(self) -> None:
        assert debugger.call_before(_Replay(), 0x24000103) == "writer"


class TestTheServerLogGivesTheStopReason:
    def test_one_value_per_stop_from_the_offset(self, tmp_path) -> None:
        server = runner.GdbServer(SimpleNamespace(), 0, attach=True)
        server.log_path = tmp_path / "server.log"
        text = (FIXTURES / "gdbserver_log16_h7_watch.txt").read_text()
        server.log_path.write_text(text)
        assert server.halt_reasons_since(0) == [5, 5, 5]
        second = text.index("[0.991]")
        assert server.halt_reasons_since(len(text[:second].encode())) == [5, 5]
        server.log_path = None

    def test_no_log_gives_nothing(self) -> None:
        assert runner.GdbServer(SimpleNamespace(), 0).halt_reasons_since(0) == []

    def test_the_bits_are_named(self) -> None:
        assert debugger.dfsr_names(0x3) == ["HALTED", "BKPT"]
        assert debugger.dfsr_names(0x5) == ["HALTED", "DWTTRAP"]


@pytest.mark.parametrize("line", ["(gdb) ", ""])
def test_the_prompt_is_not_a_record(line) -> None:
    assert gdbmi.parse(line) is None
