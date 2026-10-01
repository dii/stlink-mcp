/* STM32F407: linked for internal flash at 0x08000000, stack at the top of SRAM1 and SRAM2. */
MEMORY { FLASH (rx) : ORIGIN = 0x08000000, LENGTH = 64K }
_estack = 0x20020000;
SECTIONS {
  .isr_vector : { KEEP(*(.isr_vector)) } > FLASH
  .text : { *(.text*) *(.rodata*) } > FLASH
  /DISCARD/ : { *(.data*) *(.bss*) *(COMMON) *(.ARM.exidx*) *(.ARM.attributes) *(.comment) }
}
