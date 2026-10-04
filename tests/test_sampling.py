"""SV2, SV5: naming, summarising and the stopping condition, with no hardware.

The decoded value is g_state from tests/fixtures/elf/h7_vars_dwarf5.elf and the
bytes a NUCLEO-H743ZI2 held for it (tests/test_symbols.py's DUMP)."""

from __future__ import annotations

from pathlib import Path

import pytest

from stlink_mcp import sampling, symbols

ELF = str(Path(__file__).parent / "fixtures" / "elf" / "h7_vars_dwarf5.elf")
DUMP = bytes.fromhex("2efbffff0000c03f03008d00efbe3d013f014001410100004433221120010024fd070000")


def _g_state() -> dict:
    variable = symbols.find_variable(ELF, "g_state")
    return sampling.leaves("g_state", symbols.decode(variable.type_die, DUMP))


def test_every_scalar_of_a_variable_gets_a_name_an_agent_can_ask_for() -> None:
    named = _g_state()
    assert named["g_state.mode"] == "MODE_RUN (3)"
    assert named["g_state.flags.c"] == 0xBEEF
    assert named["g_state.samples[3]"] == 321
    assert named["g_state.pos.x"] == -3
    assert all(not isinstance(v, (dict, list)) for v in named.values())


def _samples(*rows: dict) -> list[tuple[float, dict]]:
    return [(round(i * 0.4, 2), row) for i, row in enumerate(rows)]


class TestTheSummary:
    def test_a_value_that_never_changed_comes_back_as_itself(self) -> None:
        summary, hidden = sampling.summarise(_samples({"g_state.mode": "MODE_RUN (3)"}, {"g_state.mode": "MODE_RUN (3)"}))
        assert summary == {"g_state.mode": {"value": "MODE_RUN (3)", "changes": 0}} and hidden == 0

    def test_a_counter_gives_its_ends_its_range_and_when_it_first_moved(self) -> None:
        rows = [{"tick": 5}, {"tick": 5}, {"tick": 6}, {"tick": 9}]
        summary, _ = sampling.summarise(_samples(*rows))
        assert summary["tick"] == {"first": 5, "last": 9, "changes": 2, "first_change_s": 0.8,
                                   "min": 5, "max": 9, "seen": [5, 6, 9]}

    def test_an_enum_that_changes_has_a_range_by_its_numbers(self) -> None:
        rows = [{"mode": "MODE_IDLE (1)"}, {"mode": "MODE_RUN (3)"}]
        summary, _ = sampling.summarise(_samples(*rows))
        assert (summary["mode"]["min"], summary["mode"]["max"]) == (1, 3)

    def test_many_values_list_the_ones_that_changed_first_and_count_the_rest(self) -> None:
        still = {f"v{i}": 0 for i in range(60)}
        summary, hidden = sampling.summarise(_samples({**still, "moving": 1}, {**still, "moving": 2}))
        assert next(iter(summary)) == "moving" and summary["moving"]["changes"] == 1
        assert len(summary) == sampling.LEAF_LIMIT and hidden == 61 - sampling.LEAF_LIMIT


class TestUntil:
    @pytest.mark.parametrize("text, current, first, holds", [
        ("g_state.mode == MODE_RUN", "MODE_RUN (3)", "MODE_IDLE (1)", True),
        ("g_state.mode == 3", "MODE_RUN (3)", "MODE_IDLE (1)", True),
        ("g_state.mode != MODE_RUN", "MODE_RUN (3)", "MODE_RUN (3)", False),
        ("g_state.mode >= 2", "MODE_RUN (3)", "MODE_IDLE (1)", True),
        ("g_ticks >= 0x10", 15, 0, False),
        ("g_ticks >= 0x10", 16, 0, True),
        ("g_ticks changes", 7, 7, False),
        ("g_ticks changes", 8, 7, True),
        ("g_state.flag == true", True, False, False),
        ("g_state.gain < 1.6", 1.5, 1.5, True),
    ])
    def test_it_holds_when_the_value_says_so(self, text, current, first, holds) -> None:
        assert sampling.parse_condition(text).holds(current, first) is holds

    def test_an_order_on_text_is_refused_rather_than_guessed(self) -> None:
        with pytest.raises(ValueError):
            sampling.parse_condition("g_label > 3").holds("debug", "debug")

    @pytest.mark.parametrize("text", ["g_ticks ~ 3", "== 3", "g_ticks"])
    def test_a_condition_it_cannot_read_is_refused(self, text) -> None:
        with pytest.raises(ValueError):
            sampling.parse_condition(text)
