/* STM32H7: vector table in RAM, plus a segment linked for flash. run_firmware must refuse it. */
MEMORY { RAM (rwx) : ORIGIN = 0x24000000, LENGTH = 256K
         FLASH (rx) : ORIGIN = 0x08000000, LENGTH = 64K }
_estack = 0x24080000;
SECTIONS {
  .isr_vector : { KEEP(*(.isr_vector)) } > RAM
  .text : { *(.text*) *(.rodata*) } > RAM
  .extra : { KEEP(*(.extra)) } > FLASH
  /DISCARD/ : { *(.data*) *(.bss*) *(COMMON) *(.ARM.exidx*) *(.ARM.attributes) *(.comment) }
}
