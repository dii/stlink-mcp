/* STM32H7 AXI SRAM: RAM image with .data and .bss, for debug_target.c. */
MEMORY { RAM (rwx) : ORIGIN = 0x24000000, LENGTH = 448K }
_estack = 0x24080000;
SECTIONS {
  .isr_vector : { KEEP(*(.isr_vector)) } > RAM
  .text : { *(.text*) *(.rodata*) } > RAM
  .data : { *(.data*) } > RAM
  .bss : { _sbss = .; *(.bss*) *(COMMON) _ebss = .; } > RAM
  /DISCARD/ : { *(.ARM.exidx*) }
}
