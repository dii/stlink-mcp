/* STM32H7: vector table in RAM, plus a segment at 0x2C000000, inside the SRAM
   region the tool accepts and holding nothing on an H7. GDB's load fails there. */
MEMORY { RAM (rwx) : ORIGIN = 0x24000000, LENGTH = 256K
         HOLE (rw) : ORIGIN = 0x2C000000, LENGTH = 64K }
_estack = 0x24080000;
SECTIONS {
  .isr_vector : { KEEP(*(.isr_vector)) } > RAM
  .text : { *(.text*) *(.rodata*) } > RAM
  .extra : { KEEP(*(.extra)) } > HOLE
  /DISCARD/ : { *(.data*) *(.bss*) *(COMMON) *(.ARM.exidx*) *(.ARM.attributes) *(.comment) }
}
