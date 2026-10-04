/* For the tests of flash_firmware's ELF and HEX reading: code and the flash
 * copy of .data side by side, which make one run of bytes, and a word 16 KB
 * on, which makes a second. flash_runs.hex and flash_runs.bin are objcopy's
 * -O ihex and -O binary of flash_runs.elf. */
#include <stdint.h>
volatile uint32_t g_init[4] = { 0xA1A2A3A4u, 0xB1B2B3B4u, 0xC1C2C3C4u, 0xD1D2D3D4u };
volatile uint32_t g_zero[4];
__attribute__((section(".config"), used)) const uint32_t g_config[2] = { 0x5A5A0001u, 0x12345678u };
extern uint32_t _estack, _sidata, _sdata, _edata, _sbss, _ebss;
void reset_handler(void) {
    uint32_t *s = &_sidata, *d = &_sdata;
    while (d < &_edata) *d++ = *s++;
    for (d = &_sbss; d < &_ebss; d++) *d = 0;
    for (;;) { g_zero[0]++; g_init[1] += g_zero[0] & 1; }
}
__attribute__((section(".isr_vector"), used)) void (*const vectors[2])(void) = { (void (*)(void))&_estack, reset_handler };
