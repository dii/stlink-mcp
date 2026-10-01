/* STM32H7: everything in AXI SRAM, vector table at its base. */
MEMORY { RAM (rwx) : ORIGIN = 0x24000000, LENGTH = 256K }
_estack = 0x24080000;
SECTIONS {
  .isr_vector : { KEEP(*(.isr_vector)) } > RAM
  .text : { *(.text*) *(.rodata*) } > RAM
  /DISCARD/ : { *(.data*) *(.bss*) *(COMMON) *(.ARM.exidx*) *(.ARM.attributes) *(.comment) }
}
