/* STM32F407: SRAM1 and SRAM2, contiguous from 0x20000000, vector table at its base. */
MEMORY { RAM (rwx) : ORIGIN = 0x20000000, LENGTH = 112K }
_estack = 0x20020000;
SECTIONS {
  .isr_vector : { KEEP(*(.isr_vector)) } > RAM
  .text : { *(.text*) *(.rodata*) } > RAM
  /DISCARD/ : { *(.data*) *(.bss*) *(COMMON) *(.ARM.exidx*) *(.ARM.attributes) *(.comment) }
}
