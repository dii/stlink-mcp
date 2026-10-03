"""GDB/MI parsing and the stop helpers, against output captured from a
NUCLEO-H743ZI2 on 2026-10-02.

gdbmi_h7_transcript.txt is everything GDB printed in one session through
ST-LINK_gdbserver in attach mode, on the H7 running tools/hwcheck's h7_vars: the
memory map, the breakpoint and watchpoint units, a breakpoint at writer with its
call stack, a watchpoint on g_watched, and an interrupted run.
gdbserver_log16_h7_watch.txt is the server's own log at level 16 around three
watchpoint stops.
"""

from __future__ import annotations

import queue
import subprocess
import time
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


CASES = (FIXTURES / "gdbmi_run_until_cases.txt").read_text()


def _recorded(name: str) -> dict[str, list[list[str]]]:
    """What GDB printed for each command in one recorded run_until, in order."""
    section = CASES.split(f"\n## {name}:", 1)[1].split("\n## ", 1)[0]
    answers: dict[str, list[list[str]]] = {}
    lines: list[str] | None = None
    for line in section.splitlines()[1:]:
        if line.startswith("> "):
            command = "-target-select" if line.startswith("> -target-select") else line[2:]
            lines = []
            answers.setdefault(command, []).append(lines)
        elif lines is not None:
            lines.append(line)
    return answers


class _Gdb:
    """The GDB process, replayed: each command a Session writes is answered with
    what GDB printed for it in the recording, so the real Session code reads
    real output. A command the recording does not have fails the test."""

    def __init__(self, answers: dict[str, list[list[str]]]) -> None:
        self.answers, self.sent = answers, []
        self.out: queue.Queue[str | None] = queue.Queue()
        self.stdin, self.stdout = self, iter(self.out.get, None)

    def write(self, text: str) -> None:
        command = text.rstrip("\n")
        self.sent.append(command)
        key = "-target-select" if command.startswith("-target-select") else command
        if not self.answers.get(key):
            raise AssertionError(f"{command!r} is not in the recording")
        for line in self.answers[key].pop(0):
            self.out.put(line + "\n")

    def flush(self) -> None:
        pass

    def wait(self, timeout: float | None = None) -> int:
        self.out.put(None)
        return 0


class _LoggedServer:
    def log_size(self) -> int:
        return 0

    def halt_reasons_since(self, offset: int) -> list[int]:
        return []


def _replay(monkeypatch, name: str, **overrides: list[str]) -> tuple[gdbmi.Session, _Gdb]:
    answers = _recorded(name)
    for command, lines in overrides.items():
        answers[command] = [lines]
    gdb = _Gdb(answers)
    process = SimpleNamespace(**{name: getattr(subprocess, name) for name in ("PIPE", "STDOUT", "TimeoutExpired")})
    process.Popen = lambda *a, **kw: gdb
    monkeypatch.setattr(gdbmi, "subprocess", process)
    return gdbmi.Session(Path("arm-none-eabi-gdb"), None, 61234), gdb


class TestRunUntilRefusesWhatWouldNotStopTheCore:
    """debugger.run_until against GDB's own answers, from the H7 and the F407 on
    2026-10-03. The contract tests fake this function whole, so until these
    nothing ran its refusals."""

    def test_a_breakpoint_gdb_cannot_insert_fails_at_once_with_gdbs_reason(self, monkeypatch) -> None:
        """GDB said ^running, then that it could not insert the breakpoint. Read
        as a run, it waited out the time limit and an interrupt, 12 s in all,
        and reported a target that did not stop, which had never run."""
        session, gdb = _replay(monkeypatch, "h7-unmapped")
        started = time.monotonic()
        with pytest.raises(debugger.DebugError, match="did not run the program: Cannot insert breakpoint 1. "
                                                      "Cannot access memory at address 0x60000000"):
            debugger.run_until(session, _LoggedServer(), location="0x60000000", watch=None, seconds=2)
        assert time.monotonic() - started < 1 and "-exec-interrupt" not in gdb.sent

    def test_a_location_gdb_cannot_find_says_what_gdb_said(self, monkeypatch) -> None:
        session, _ = _replay(monkeypatch, "h7-no-function")
        with pytest.raises(debugger.DebugError, match='Function \\\\?"no_such_function\\\\?" not defined'):
            debugger.run_until(session, _LoggedServer(), location="no_such_function", watch=None, seconds=2)

    def test_a_watch_gdb_would_do_by_single_stepping_is_refused(self, monkeypatch) -> None:
        session, gdb = _replay(monkeypatch, "h7-register")
        with pytest.raises(debugger.DebugError, match="software watchpoint"):
            debugger.run_until(session, _LoggedServer(), location=None, watch="$pc", seconds=2)
        assert "-exec-continue" not in gdb.sent

    def test_a_watch_wider_than_the_server_watches_is_refused_before_it_is_set(self, monkeypatch) -> None:
        """GDB took a 36-byte g_state as one hardware watchpoint; ST's server
        logged "Unsupported value of length (36), patching to 1-byte", replied OK,
        and the run reported no write in 3 s while the program wrote g_state.samples
        on every tick, on the H7 and the F407. 8 bytes went the same way."""
        session, gdb = _replay(monkeypatch, "h7-struct")
        with pytest.raises(debugger.DebugError, match="g_state is 36 bytes at 0x24000130; a watchpoint covers 1, 2 or 4"):
            debugger.run_until(session, _LoggedServer(), location=None, watch="g_state", seconds=3)
        assert not [c for c in gdb.sent if c.startswith("-break-watch")]

    def test_a_watch_not_aligned_to_its_size_is_refused(self, monkeypatch) -> None:
        """Two bytes at 0x2400013D, one of them g_state.samples[0]'s low byte,
        written every fourth tick: the server took the watch without a warning
        and it never stopped the core in 5 s, on the H7 or the F407."""
        session, gdb = _replay(monkeypatch, "h7-misaligned")
        with pytest.raises(debugger.DebugError, match="2 bytes at 0x2400013D, which is not aligned"):
            debugger.run_until(session, _LoggedServer(), location=None,
                               watch="*(unsigned short *)(0x24000000+0x13D)", seconds=5)
        assert not [c for c in gdb.sent if c.startswith("-break-watch")]

    def test_a_breakpoint_the_unit_cannot_reach_is_refused(self, monkeypatch) -> None:
        """The F407's unit is revision 1. ST's server marks RAM read-write, so GDB
        asks for a software breakpoint at writer there; without a map, which is
        GDB's answer below (captured from GDB with no target), it would ask for
        a hardware one that never fires."""
        no_map = ['~"Using memory regions provided by the target.\\n"',
                  '~"There are no memory regions defined.\\n"', "^done"]
        session, gdb = _replay(monkeypatch, "f4-writer", **{'-interpreter-exec console "info mem"': no_map})
        with pytest.raises(debugger.DebugError, match="revision 1\\) covers only addresses below 0x20000000"):
            debugger.run_until(session, _LoggedServer(), location="writer", watch=None, seconds=5)
        assert "-exec-continue" not in gdb.sent

    def test_the_servers_map_marks_what_is_not_flash_read_write(self, monkeypatch) -> None:
        """So "software" says what GDB asks for, not that there is RAM: on the
        F407 nothing answers at 0x60000000 either."""
        session, _ = _replay(monkeypatch, "f4-writer")
        assert debugger.writable(session, 0x60000000) is True


class TestAWatchpointNamesNoWriterItCannotKnow:
    """The core stops one to four instructions after the write. PC and LR alone
    gave the same picture for a write by the function that returned and for a
    write by its caller, so a returned call is offered as a candidate only."""

    def _watch(self, monkeypatch, name: str, expression: str) -> debugger.Stop:
        session, _ = _replay(monkeypatch, name)
        return debugger.run_until(session, _LoggedServer(), location=None, watch=expression, seconds=5)

    def test_a_call_the_core_is_still_in_is_not_named(self, monkeypatch) -> None:
        """reset_handler wrote, then called level1; the H7 stopped inside it, and
        level1 was named as the call before LR."""
        stop = self._watch(monkeypatch, "h7-member2", "g_state.samples[1]")
        assert stop.reached and stop.frames[0]["function"] == "level1"
        assert "returned_from" not in stop.detail and "called_just_before" not in stop.detail

    def test_the_function_that_wrote_and_returned_is_named(self, monkeypatch) -> None:
        stop = self._watch(monkeypatch, "h7-watched", "g_watched")
        assert stop.frames[0]["function"] == "reset_handler" and stop.detail["returned_from"] == "writer"

    def test_a_returned_call_is_a_candidate_even_when_the_caller_wrote(self, monkeypatch) -> None:
        """reset_handler wrote here; LR was left from the call at tick five. This
        is why the result says it cannot tell which."""
        stop = self._watch(monkeypatch, "f4-member2", "g_state.samples[1]")
        assert stop.frames[0]["function"] == "reset_handler"
        assert stop.detail["returned_from"] == "reached_tick_five"
        from stlink_mcp import server
        assert "stopped_at" in server._WRITE_NOTE and "Nothing here tells which" in server._WRITE_NOTE


class TestAWatchedPeripheralRegisterIsNotReadByThisTool:
    """GDB read 0x40004828 three times when it set the watchpoint there, on the
    H7 on 2026-10-03, before the watchpoint itself, so a register whose read
    changes state is refused before GDB is asked; any other peripheral register
    is watched, and this tool reads its value no further."""

    TDR = "0x40004828"

    def test_a_register_known_to_change_when_read_is_refused_before_gdb_sees_it(self, monkeypatch) -> None:
        session, gdb = _replay(monkeypatch, "h7-watch-tdr")
        effect = {"register": "USART3.TDR", "effect": "clears a flag", "provenance": "measured",
                  "established": "a test"}
        with pytest.raises(debugger.DebugError, match="GDB reads a watched address when it sets the watchpoint"):
            debugger.run_until(session, _LoggedServer(), location=None, watch=self.TDR, seconds=5,
                               read_effects=lambda address, size: [effect])
        assert not [c for c in gdb.sent if c.startswith("-break-watch")]

    def test_any_other_is_watched_and_its_value_left_alone(self, monkeypatch) -> None:
        session, gdb = _replay(monkeypatch, "h7-watch-tdr")
        stop = debugger.run_until(session, _LoggedServer(), location=None, watch=self.TDR, seconds=5,
                                  read_effects=lambda address, size: [])
        assert stop.reached and "value_before" not in stop.detail["watched"]
        assert "GDB read it when it set the watchpoint" in stop.detail["watched"]["value_note"]
        assert not [c for c in gdb.sent if c.startswith("-data-evaluate-expression") and "sizeof" not in c and "&(" not in c]

