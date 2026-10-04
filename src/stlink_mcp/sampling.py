"""Values sampled while the program runs: the summary and the stopping condition.

Agents in an evaluation answered "is it running, and what is it doing?" by
reading one counter twice in a row and comparing the two (2026-10-04).
sample_values does that for them: several values read together, again and
again, without halting the core, summarised. This module is the part with no
hardware in it: naming each leaf of a decoded value, the summary, and `until`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: The most leaves one result lists; the rest are counted.
LEAF_LIMIT = 48

#: How many distinct values a changing leaf lists before it gives only a count.
SEEN_LIMIT = 6


def leaves(name: str, value: Any) -> dict[str, Any]:
    """A decoded value as named scalars: "g_state.pos.x", "g_state.samples[2]"."""
    out: dict[str, Any] = {}

    def walk(path: str, v: Any) -> None:
        if isinstance(v, dict):
            for key, item in v.items():
                walk(f"{path}.{key}", item)
        elif isinstance(v, list):
            for i, item in enumerate(v):
                walk(f"{path}[{i}]", item)
        else:
            out[path] = v
    walk(name, value)
    return out


def numeric(v: Any) -> float | int | None:
    """A leaf as a number where it is one: an enum's "MODE_RUN (3)" is 3."""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str) and (m := re.fullmatch(r".* \((-?\d+)\)", v)):
        return int(m.group(1))
    return None


_COMPARE = re.compile(r"^\s*(\S+?)\s*(==|!=|<=|>=|<|>)\s*(\S.*?)\s*$")
_CHANGES = re.compile(r"^\s*(\S+)\s+changes\s*$")


@dataclass(frozen=True)
class Condition:
    """`until`: a watched leaf compared with a value, or "<leaf> changes"."""
    leaf: str
    op: str
    operand: Any = None

    def holds(self, current: Any, first: Any) -> bool:
        if self.op == "changes":
            return current != first
        want, have = self.operand, current
        if self.op in ("==", "!="):
            same = (have == want or numeric(have) == want if isinstance(want, (int, float))
                    else have == want or (isinstance(have, str) and have.split(" (")[0] == want))
            return same if self.op == "==" else not same
        a, b = numeric(have), want
        if a is None or not isinstance(b, (int, float)):
            raise ValueError(f"until compares {self.leaf} with {self.op}, but its value {have!r} "
                             "is not a number.")
        return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[self.op]

    def __str__(self) -> str:
        return f"{self.leaf} changes" if self.op == "changes" else f"{self.leaf} {self.op} {self.operand}"


def parse_condition(text: str) -> Condition:
    """"g_state.mode == MODE_RUN", "g_ticks >= 100", "0x24070000 != 0", "g_ticks changes"."""
    if (m := _CHANGES.match(text)):
        return Condition(m.group(1), "changes")
    m = _COMPARE.match(text)
    if not m:
        raise ValueError(f"until {text!r} is not '<value> <op> <number or name>' with op one of "
                         "== != < <= > >=, or '<value> changes'.")
    raw = m.group(3)
    operand: Any = raw
    for convert in (lambda s: int(s, 0), float):
        try:
            operand = convert(raw)
            break
        except ValueError:
            continue
    return Condition(m.group(1), m.group(2), operand)


def summarise(samples: list[tuple[float, dict[str, Any]]]) -> tuple[dict[str, Any], int]:
    """Each leaf over the samples: its value if it never changed, else first,
    last, range, how many samples changed it and when it first did. The leaves
    that changed come first; past LEAF_LIMIT the rest are counted, not listed."""
    names = list(samples[0][1])
    out: dict[str, dict[str, Any]] = {}
    for name in names:
        seq = [(t, values.get(name)) for t, values in samples]
        changes = [t for (_, a), (t, b) in zip(seq, seq[1:]) if a != b]
        if not changes:
            out[name] = {"value": seq[0][1], "changes": 0}
            continue
        entry: dict[str, Any] = {"first": seq[0][1], "last": seq[-1][1], "changes": len(changes),
                                 "first_change_s": round(changes[0], 2)}
        numbers = [numeric(v) for _, v in seq]
        if all(n is not None for n in numbers):
            entry["min"], entry["max"] = min(numbers), max(numbers)
        seen = list(dict.fromkeys(v for _, v in seq))
        if len(seen) <= SEEN_LIMIT:
            entry["seen"] = seen
        else:
            entry["distinct"] = len(seen)
        out[name] = entry
    ordered = sorted(out, key=lambda n: out[n]["changes"] == 0)
    shown = {n: out[n] for n in ordered[:LEAF_LIMIT]}
    return shown, len(ordered) - len(shown)
