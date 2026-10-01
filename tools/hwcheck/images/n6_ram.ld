/* STM32N6: AXISRAM2, vector table 0x400 into the load base, as the boot ROM expects. */
MEMORY { RAM (rwx) : ORIGIN = 0x34180400, LENGTH = 128K }
_estack = 0x341C0000;
SECTIONS {
  .isr_vector : { KEEP(*(.isr_vector)) } > RAM
  .text : { *(.text*) *(.rodata*) } > RAM
  /DISCARD/ : { *(.data*) *(.bss*) *(COMMON) *(.ARM.exidx*) *(.ARM.attributes) *(.comment) }
}
