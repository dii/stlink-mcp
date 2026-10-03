"""GDB's machine interface, GDB/MI, for the tools that stop the target.

A batch GDB, which `run_firmware` uses to load, blocks on `continue` until the
target stops, so nothing can bound the wait. GDB/MI kept open beside the GDB
server can: `-exec-continue`, then `-exec-interrupt` when the time is up. That
works only when `mi-async` is set before connecting; set afterwards, the
interrupt is never sent (measured 2026-10-02 on an STM32H7).

The output grammar is GDB's own, documented under "GDB/MI Output Syntax": a
result record `^done,name=value,...`, async records `*stopped,...`, and stream
records `~"text"`, where a value is a C string, a tuple `{...}` or a list
`[...]`.
"""

from __future__ import annotations

import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class MiError(RuntimeError):
    pass


def _cstring(text: str, i: int) -> tuple[str, int]:
    assert text[i] == '"'
    out, i = [], i + 1
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(nxt, nxt))
            i += 2
            continue
        if ch == '"':
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    raise MiError(f"unterminated string in GDB/MI output: {text[:120]}")


def _value(text: str, i: int) -> tuple[Any, int]:
    ch = text[i]
    if ch == '"':
        return _cstring(text, i)
    if ch == "{":
        return _tuple(text, i)
    if ch == "[":
        return _list(text, i)
    raise MiError(f"unexpected {ch!r} at {i} in GDB/MI output: {text[:120]}")


def _result(text: str, i: int) -> tuple[str, Any, int]:
    eq = text.index("=", i)
    name = text[i:eq]
    value, i = _value(text, eq + 1)
    return name, value, i


def _tuple(text: str, i: int) -> tuple[dict[str, Any], int]:
    out: dict[str, Any] = {}
    i += 1
    if text[i] == "}":
        return out, i + 1
    while True:
        name, value, i = _result(text, i)
        out[name] = value
        if text[i] == "}":
            return out, i + 1
        i += 1  # ","


def _list(text: str, i: int) -> tuple[list[Any], int]:
    out: list[Any] = []
    i += 1
    if text[i] == "]":
        return out, i + 1
    while True:
        if text[i] in '"{[':
            value, i = _value(text, i)
            out.append(value)
        else:  # a list of results, as in frame=[...] lists of name=value
            name, value, i = _result(text, i)
            out.append({name: value})
        if text[i] == "]":
            return out, i + 1
        i += 1


@dataclass
class Record:
    kind: str
    """'^' result, '*' exec async, '=' notify, '~' console, '@' target, '&' log."""
    klass: str
    results: dict[str, Any] = field(default_factory=dict)
    text: str = ""


def parse(line: str) -> Record | None:
    """One line of GDB/MI output, or None for the prompt and blank lines."""
    line = line.rstrip("\r\n")
    if not line or line.startswith("(gdb)"):
        return None
    i = 0
    while i < len(line) and line[i].isdigit():  # a command token
        i += 1
    kind = line[i]
    if kind in "~@&":
        text, _ = _cstring(line, i + 1)
        return Record(kind, "", text=text)
    if kind not in "^*=+":
        return Record("?", "", text=line)
    rest = line[i + 1:]
    klass, _, tail = rest.partition(",")
    results: dict[str, Any] = {}
    j = 0
    while j < len(tail):
        name, value, j = _result(tail, j)
        results[name] = value
        if j < len(tail) and tail[j] == ",":
            j += 1
    return Record(kind, klass, results)


class Session:
    """One GDB/MI process, connected to a GDB server on localhost."""

    def __init__(self, gdb: Path, elf: str | None, port: int, timeout: float = 30) -> None:
        argv = [str(gdb), "-q", "--nx", "--interpreter=mi3"] + ([elf] if elf else [])
        self._proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        self._lines: queue.Queue[str] = queue.Queue()
        self.transcript: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()
        self.command("-gdb-set mi-async on")
        self.command("-gdb-set confirm off")
        self.command("-gdb-set pagination off")
        connected = self.command(f"-target-select extended-remote localhost:{port}", timeout=timeout)
        if connected.klass != "connected":
            raise MiError(f"GDB could not connect to the server on port {port}: {connected.results}")

    def _read(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            self._lines.put(line)

    def _next(self, deadline: float) -> Record | None:
        while True:
            try:
                line = self._lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                return None
            self.transcript.append(line.rstrip("\n"))
            record = parse(line)
            if record is not None:
                return record

    def command(self, text: str, timeout: float = 30) -> Record:
        """Send one command and return its result record; async records that
        arrive meanwhile are kept for wait_stopped."""
        assert self._proc.stdin is not None
        self._proc.stdin.write(text + "\n")
        self._proc.stdin.flush()
        deadline = time.monotonic() + timeout
        console: list[str] = []
        while (record := self._next(deadline)) is not None:
            if record.kind == "^":
                if record.klass == "error":
                    raise MiError(f"{text}: {record.results.get('msg', record.results)}")
                record.text = "".join(console)
                return record
            if record.kind == "~":
                console.append(record.text)
            if record.kind == "*" and record.klass == "stopped":
                self._pending_stop = record
        raise MiError(f"{text}: no answer from GDB within {timeout:g} s")

    _pending_stop: Record | None = None

    def resume(self) -> None:
        """Continue, forgetting any stop already reported. GDB reports a stop when
        it attaches; kept, it would read as this continue's stop at once
        (measured 2026-10-02, the first time this was run)."""
        self._pending_stop = None
        self.command("-exec-continue")

    def wait_stopped(self, timeout: float) -> Record | None:
        """The next stop, or None after `timeout`. GDB answers a continue with
        ^running before it inserts the breakpoints, so one it cannot insert
        arrives here as ^error, with the reason in its log stream (measured
        2026-10-03); that raises MiError, and the target was not resumed."""
        if self._pending_stop is not None:
            stop, self._pending_stop = self._pending_stop, None
            return stop
        deadline = time.monotonic() + timeout
        log: list[str] = []
        while (record := self._next(deadline)) is not None:
            if record.kind == "*" and record.klass == "stopped":
                return record
            if record.kind == "&" and record.text.strip() not in ("", "Warning:"):
                log.append(record.text.strip())
            if record.kind == "^" and record.klass == "error":
                raise MiError(" ".join(log) or str(record.results.get("msg", record.results)))
        return None

    def close(self, detach: bool) -> None:
        """Detach resumes the core; disconnect leaves it as it is (measured)."""
        try:
            self.command("-target-detach" if detach else "-target-disconnect", timeout=10)
            self.command("-gdb-exit", timeout=5)
        except MiError:
            pass
        finally:
            try:
                self._proc.wait(5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
