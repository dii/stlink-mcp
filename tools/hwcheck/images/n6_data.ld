/* STM32N6: AXISRAM2 image with .data and .bss, for debug_target.c; vector table 0x400 into the load base. */
MEMORY { RAM (rwx) : ORIGIN = 0x34180400, LENGTH = 128K }
_estack = 0x341C0000;
SECTIONS {
  .isr_vector : { KEEP(*(.isr_vector)) } > RAM
  .text : { *(.text*) *(.rodata*) } > RAM
  .data : { *(.data*) } > RAM
  .bss : { _sbss = .; *(.bss*) *(COMMON) _ebss = .; } > RAM
  /DISCARD/ : { *(.ARM.exidx*) }
}
