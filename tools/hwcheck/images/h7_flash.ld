/* STM32H7: linked for internal flash, as a default build is. */
MEMORY { FLASH (rx) : ORIGIN = 0x08000000, LENGTH = 64K }
_estack = 0x24080000;
SECTIONS {
  .isr_vector : { KEEP(*(.isr_vector)) } > FLASH
  .text : { *(.text*) *(.rodata*) } > FLASH
  /DISCARD/ : { *(.data*) *(.bss*) *(COMMON) *(.ARM.exidx*) *(.ARM.attributes) *(.comment) }
}
