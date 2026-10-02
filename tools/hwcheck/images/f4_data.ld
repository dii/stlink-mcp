/* STM32F407 SRAM1 and SRAM2: RAM image with .data and .bss, for debug_target.c. */
MEMORY { RAM (rwx) : ORIGIN = 0x20000000, LENGTH = 112K }
_estack = 0x20020000;
SECTIONS {
  .isr_vector : { KEEP(*(.isr_vector)) } > RAM
  .text : { *(.text*) *(.rodata*) } > RAM
  .data : { *(.data*) } > RAM
  .bss : { _sbss = .; *(.bss*) *(COMMON) _ebss = .; } > RAM
  /DISCARD/ : { *(.ARM.exidx*) }
}
