"""Tests for the pure fault-register decoding logic.

Every test here runs without hardware. The input is a dict of address-to-word
values matching what Probe.read_memory returns, and the output is a decoded
FaultStatus. A wrong decode is worse than no decode, because it tells an
agent a definitive wrong reason for a crash.
"""

import pytest

from stlink_mcp import faults


class TestNoFault:
    def test_all_zeros_means_no_fault(self) -> None:
        words = {addr: 0 for addr in range(faults.CFSR, faults.AFSR + 4, 4)}
        status = faults.decode(words)
        assert not status.any_fault
        assert status.faults == []
        assert status.mmfar is None
        assert status.bfar is None
        assert "no fault" in status.diagnosis

    def test_missing_registers_default_to_zero(self) -> None:
        status = faults.decode({})
        assert not status.any_fault

    def test_dfsr_bits_are_not_faults(self) -> None:
        status = faults.decode({faults.DFSR: 0x1})
        assert not status.any_fault
        assert len(status.debug) == 1
        assert status.debug[0].name == "HALTED"


class TestBusFaults:
    def test_precise_bus_error_with_address(self) -> None:
        cfsr = (1 << 9) | (1 << 15)  # PRECISERR + BFARVALID
        words = {faults.CFSR: cfsr, faults.BFAR: 0x40001234}
        status = faults.decode(words)
        assert status.any_fault
        assert any(f.name == "PRECISERR" for f in status.faults)
        assert status.bfar == 0x40001234
        assert "0x40001234" in status.diagnosis

    def test_precise_bus_error_without_valid_address(self) -> None:
        cfsr = 1 << 9  # PRECISERR only, no BFARVALID
        words = {faults.CFSR: cfsr, faults.BFAR: 0xDEADBEEF}
        status = faults.decode(words)
        assert status.bfar is None
        assert any(f.name == "PRECISERR" for f in status.faults)

    def test_imprecise_bus_error(self) -> None:
        cfsr = 1 << 10  # IMPRECISERR
        status = faults.decode({faults.CFSR: cfsr})
        assert any(f.name == "IMPRECISERR" for f in status.faults)

    def test_instruction_bus_error(self) -> None:
        cfsr = 1 << 8  # IBUSERR
        status = faults.decode({faults.CFSR: cfsr})
        assert any(f.name == "IBUSERR" for f in status.faults)

    def test_stacking_bus_fault(self) -> None:
        cfsr = 1 << 12  # STKERR
        status = faults.decode({faults.CFSR: cfsr})
        assert any(f.name == "STKERR" for f in status.faults)

    def test_unstacking_bus_fault(self) -> None:
        cfsr = 1 << 11  # UNSTKERR
        status = faults.decode({faults.CFSR: cfsr})
        assert any(f.name == "UNSTKERR" for f in status.faults)

    def test_fp_lazy_stacking(self) -> None:
        cfsr = 1 << 13  # LSPERR
        status = faults.decode({faults.CFSR: cfsr})
        assert any(f.name == "LSPERR" for f in status.faults)


class TestMemManageFaults:
    def test_data_access_violation_with_address(self) -> None:
        cfsr = (1 << 1) | (1 << 7)  # DACCVIOL + MMARVALID
        words = {faults.CFSR: cfsr, faults.MMFAR: 0x20100000}
        status = faults.decode(words)
        assert any(f.name == "DACCVIOL" for f in status.faults)
        assert status.mmfar == 0x20100000
        assert "0x20100000" in status.diagnosis

    def test_instruction_access_violation_with_address(self) -> None:
        cfsr = (1 << 0) | (1 << 7)  # IACCVIOL + MMARVALID
        words = {faults.CFSR: cfsr, faults.MMFAR: 0x00000000}
        status = faults.decode(words)
        assert any(f.name == "IACCVIOL" for f in status.faults)
        assert status.mmfar == 0

    def test_stacking_memmanage(self) -> None:
        cfsr = 1 << 4  # MSTKERR
        status = faults.decode({faults.CFSR: cfsr})
        assert any(f.name == "MSTKERR" for f in status.faults)

    def test_unstacking_memmanage(self) -> None:
        cfsr = 1 << 3  # MUNSTKERR (bit 3 per Arm; this test pinned bit 5 until 2026-09-03)
        status = faults.decode({faults.CFSR: cfsr})
        assert any(f.name == "MUNSTKERR" for f in status.faults)

    def test_lazy_fp_memmanage(self) -> None:
        cfsr = 1 << 5  # MLSPERR
        status = faults.decode({faults.CFSR: cfsr})
        assert any(f.name == "MLSPERR" for f in status.faults)

    def test_mmfar_ignored_when_invalid(self) -> None:
        cfsr = 1 << 1  # DACCVIOL only, no MMARVALID
        words = {faults.CFSR: cfsr, faults.MMFAR: 0x12345678}
        status = faults.decode(words)
        assert status.mmfar is None


class TestUsageFaults:
    def test_undefined_instruction(self) -> None:
        cfsr = 1 << 16  # UNDEFINSTR
        status = faults.decode({faults.CFSR: cfsr})
        assert any(f.name == "UNDEFINSTR" for f in status.faults)

    def test_invalid_state(self) -> None:
        cfsr = 1 << 17  # INVSTATE
        status = faults.decode({faults.CFSR: cfsr})
        assert any(f.name == "INVSTATE" for f in status.faults)

    def test_invalid_pc(self) -> None:
        cfsr = 1 << 18  # INVPC
        status = faults.decode({faults.CFSR: cfsr})
        assert any(f.name == "INVPC" for f in status.faults)

    def test_no_coprocessor(self) -> None:
        cfsr = 1 << 19  # NOCP
        status = faults.decode({faults.CFSR: cfsr})
        assert any(f.name == "NOCP" for f in status.faults)

    def test_stack_overflow(self) -> None:
        cfsr = 1 << 20  # STKOF
        status = faults.decode({faults.CFSR: cfsr})
        assert any(f.name == "STKOF" for f in status.faults)

    def test_unaligned_access(self) -> None:
        cfsr = 1 << 24  # UNALIGNED
        status = faults.decode({faults.CFSR: cfsr})
        assert any(f.name == "UNALIGNED" for f in status.faults)

    def test_divide_by_zero(self) -> None:
        cfsr = 1 << 25  # DIVBYZERO
        status = faults.decode({faults.CFSR: cfsr})
        assert any(f.name == "DIVBYZERO" for f in status.faults)
        assert "zero" in status.diagnosis.lower()


class TestHardFaults:
    def test_forced_escalation_with_bus_fault(self) -> None:
        cfsr = (1 << 9) | (1 << 15)  # PRECISERR + BFARVALID
        hfsr = 1 << 30  # FORCED
        words = {faults.CFSR: cfsr, faults.HFSR: hfsr, faults.BFAR: 0x40001234}
        status = faults.decode(words)
        assert any(f.name == "FORCED" for f in status.faults)
        assert any(f.name == "PRECISERR" for f in status.faults)
        assert "escalated" in status.diagnosis
        assert "0x40001234" in status.diagnosis

    def test_forced_with_no_configurable_bits(self) -> None:
        hfsr = 1 << 30
        status = faults.decode({faults.HFSR: hfsr})
        assert "forced" in status.diagnosis.lower()
        assert "no configurable" in status.diagnosis.lower()

    def test_vector_table_fault(self) -> None:
        hfsr = 1 << 1  # VECTTBL
        status = faults.decode({faults.HFSR: hfsr})
        assert any(f.name == "VECTTBL" for f in status.faults)
        assert "vector table" in status.diagnosis.lower()

    def test_debug_event(self) -> None:
        hfsr = 1 << 31  # DEBUGEVT
        status = faults.decode({faults.HFSR: hfsr})
        assert any(f.name == "DEBUGEVT" for f in status.faults)
        assert "debug" in status.diagnosis.lower()


class TestDebugFaults:
    def test_halted(self) -> None:
        status = faults.decode({faults.DFSR: 1 << 0})
        assert not status.any_fault
        assert any(d.name == "HALTED" for d in status.debug)

    def test_breakpoint(self) -> None:
        status = faults.decode({faults.DFSR: 1 << 1})
        assert any(d.name == "BKPT" for d in status.debug)

    def test_watchpoint(self) -> None:
        status = faults.decode({faults.DFSR: 1 << 2})
        assert any(d.name == "DWTTRAP" for d in status.debug)

    def test_vector_catch(self) -> None:
        status = faults.decode({faults.DFSR: 1 << 3})
        assert any(d.name == "VCATCH" for d in status.debug)

    def test_external(self) -> None:
        status = faults.decode({faults.DFSR: 1 << 4})
        assert any(d.name == "EXTERNAL" for d in status.debug)

    def test_multiple_debug_bits(self) -> None:
        status = faults.decode({faults.DFSR: 0b11111})
        assert len(status.debug) == 5


class TestCommonCrashPatterns:
    """Composed from real firmware crash scenarios."""

    def test_null_pointer_dereference(self) -> None:
        """Accessing address 0 with no MPU raises a bus fault on most parts."""
        cfsr = (1 << 9) | (1 << 15)  # PRECISERR + BFARVALID
        hfsr = 1 << 30               # FORCED
        words = {faults.CFSR: cfsr, faults.HFSR: hfsr, faults.BFAR: 0x00000000}
        status = faults.decode(words)
        assert status.bfar == 0
        assert "escalated" in status.diagnosis
        assert "0x00000000" in status.diagnosis

    def test_stack_corruption_jump_to_wrong_address(self) -> None:
        cfsr = 1 << 16  # UNDEFINSTR
        hfsr = 1 << 30  # FORCED
        status = faults.decode({faults.CFSR: cfsr, faults.HFSR: hfsr})
        assert "undefined instruction" in status.diagnosis
        assert "escalated" in status.diagnosis

    def test_function_pointer_without_thumb_bit(self) -> None:
        cfsr = 1 << 17  # INVSTATE
        hfsr = 1 << 30  # FORCED
        status = faults.decode({faults.CFSR: cfsr, faults.HFSR: hfsr})
        diag = status.diagnosis.lower()
        assert "state" in diag or "thumb" in diag
        assert "escalated" in diag

    def test_multiple_faults_simultaneously(self) -> None:
        """Multiple bits can be set if faults accumulated before clearing."""
        cfsr = (1 << 9) | (1 << 16) | (1 << 15)
        hfsr = 1 << 30
        words = {faults.CFSR: cfsr, faults.HFSR: hfsr, faults.BFAR: 0x60000000}
        status = faults.decode(words)
        root_causes = [f for f in status.faults if f.category != "HardFault"]
        assert len(root_causes) >= 2
        assert "0x60000000" in status.diagnosis


class TestAddressValidity:
    """MMARVALID and BFARVALID control whether the address registers matter."""

    def test_bfar_reported_only_when_valid(self) -> None:
        cfsr_valid = (1 << 9) | (1 << 15)   # PRECISERR + BFARVALID
        cfsr_invalid = 1 << 9                # PRECISERR only
        words_valid = {faults.CFSR: cfsr_valid, faults.BFAR: 0xCAFE0000}
        words_invalid = {faults.CFSR: cfsr_invalid, faults.BFAR: 0xCAFE0000}
        assert faults.decode(words_valid).bfar == 0xCAFE0000
        assert faults.decode(words_invalid).bfar is None

    def test_mmfar_reported_only_when_valid(self) -> None:
        cfsr_valid = (1 << 1) | (1 << 7)    # DACCVIOL + MMARVALID
        cfsr_invalid = 1 << 1               # DACCVIOL only
        words_valid = {faults.CFSR: cfsr_valid, faults.MMFAR: 0xBEEF0000}
        words_invalid = {faults.CFSR: cfsr_invalid, faults.MMFAR: 0xBEEF0000}
        assert faults.decode(words_valid).mmfar == 0xBEEF0000
        assert faults.decode(words_invalid).mmfar is None

    def test_address_zero_is_valid_when_flagged(self) -> None:
        """Address 0 is a real fault address (null pointer), not missing."""
        cfsr = (1 << 9) | (1 << 15)  # PRECISERR + BFARVALID
        status = faults.decode({faults.CFSR: cfsr, faults.BFAR: 0})
        assert status.bfar == 0


class TestRawRegisterValues:
    def test_all_six_registers_present(self) -> None:
        words = {faults.CFSR: 0x12345678, faults.HFSR: 0xABCDEF01}
        status = faults.decode(words)
        assert set(status.raw.keys()) == {
            "CFSR", "HFSR", "DFSR", "MMFAR", "BFAR", "AFSR",
        }

    def test_raw_values_match_input(self) -> None:
        words = {faults.CFSR: 0x42, faults.HFSR: 0x99, faults.DFSR: 0x07}
        status = faults.decode(words)
        assert status.raw["CFSR"] == 0x42
        assert status.raw["HFSR"] == 0x99
        assert status.raw["DFSR"] == 0x07

    def test_afsr_is_passed_through(self) -> None:
        words = {faults.AFSR: 0xCAFEBABE}
        status = faults.decode(words)
        assert status.afsr == 0xCAFEBABE


class TestRegisterAddresses:
    """These are from the ARM architecture reference and must be exact."""

    def test_cfsr(self) -> None:
        assert faults.CFSR == 0xE000ED28

    def test_hfsr(self) -> None:
        assert faults.HFSR == 0xE000ED2C

    def test_dfsr(self) -> None:
        assert faults.DFSR == 0xE000ED30

    def test_mmfar(self) -> None:
        assert faults.MMFAR == 0xE000ED34

    def test_bfar(self) -> None:
        assert faults.BFAR == 0xE000ED38

    def test_afsr(self) -> None:
        assert faults.AFSR == 0xE000ED3C

    def test_region_covers_all(self) -> None:
        assert faults.FAULT_REGION_BASE == faults.CFSR
        assert faults.FAULT_REGION_BASE + faults.FAULT_REGION_SIZE == faults.AFSR + 4


class TestEveryDefinedBitDecodesCorrectly:
    """A mechanical sweep: every bit in every definition table must produce
    exactly one ActiveBit with the correct category, name and a non-empty
    meaning."""

    _ALL_DEFS = [
        ("MemManage", faults._MMFSR_BITS, 0),      # CFSR bits [7:0]
        ("BusFault", faults._BFSR_BITS, 8),         # CFSR bits [15:8]
        ("UsageFault", faults._UFSR_BITS, 16),       # CFSR bits [31:16]
    ]

    def test_all_cfsr_bits(self) -> None:
        for category, defs, shift in self._ALL_DEFS:
            for bit, name, meaning in defs:
                cfsr = 1 << (bit + shift)
                status = faults.decode({faults.CFSR: cfsr})
                matches = [f for f in status.faults if f.name == name]
                assert len(matches) == 1, f"{name} not found for CFSR={cfsr:#x}"
                assert matches[0].category == category
                assert matches[0].meaning

    def test_all_hfsr_bits(self) -> None:
        for bit, name, meaning in faults._HFSR_BITS:
            hfsr = 1 << bit
            status = faults.decode({faults.HFSR: hfsr})
            matches = [f for f in status.faults if f.name == name]
            assert len(matches) == 1, f"{name} not found for HFSR={hfsr:#x}"
            assert matches[0].category == "HardFault"
            assert matches[0].meaning

    def test_all_dfsr_bits(self) -> None:
        for bit, name, meaning in faults._DFSR_BITS:
            dfsr = 1 << bit
            status = faults.decode({faults.DFSR: dfsr})
            matches = [d for d in status.debug if d.name == name]
            assert len(matches) == 1, f"{name} not found for DFSR={dfsr:#x}"
            assert matches[0].category == "Debug"
            assert matches[0].meaning


class TestWhichCoresHaveTheRegisters:
    """The tool refuses a core without the registers, and does not verify a
    clean read on a core it cannot place, so this classification is what
    stands between a HardFaulted Cortex-M0+ and "no fault is active"."""

    @pytest.mark.parametrize("cpu", [
        "Cortex-M3", "Cortex-M4", "Cortex-M7", "Cortex-M33", "Cortex-M35P",
        "Cortex-M52", "Cortex-M55", "Cortex-M85",
    ])
    def test_armv7m_and_armv8m_mainline_have_them(self, cpu: str) -> None:
        assert faults.has_fault_registers(cpu) is True

    @pytest.mark.parametrize("cpu", ["Cortex-M0", "Cortex-M0+", "Cortex-M1", "Cortex-M23"])
    def test_armv6m_and_armv8m_baseline_do_not(self, cpu: str) -> None:
        assert faults.has_fault_registers(cpu) is False

    @pytest.mark.parametrize("cpu", [None, "", "Cortex-A7", "something new"])
    def test_anything_else_is_unknown_rather_than_guessed(self, cpu) -> None:
        assert faults.has_fault_registers(cpu) is None

    def test_m3_does_not_match_inside_m33(self) -> None:
        """The digit lookahead is what keeps these apart."""
        assert faults.may_bank_by_security("Cortex-M33") is True
        assert faults.may_bank_by_security("Cortex-M3") is False
        assert faults.may_bank_by_security("Cortex-M7") is False


class TestMissingRegisters:
    def test_a_full_read_misses_nothing(self) -> None:
        words = {addr: 0 for addr in range(faults.CFSR, faults.AFSR + 4, 4)}
        assert faults.missing_registers(words) == []

    def test_absent_words_are_named_in_address_order(self) -> None:
        assert faults.missing_registers({faults.HFSR: 0, faults.BFAR: 0}) == [
            "CFSR", "DFSR", "MMFAR", "AFSR",
        ]
