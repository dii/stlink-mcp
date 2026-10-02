/* A program to debug, for tools/hwcheck and the version 1.0 tests: typed globals to
   read by name, a variable written by one function to watch, a function reached
   at tick five, and a three-deep call chain that hangs when built with -DHANG.
   Unlike beacon.c it has .data and .bss, which its reset handler sets up. */
#include <stdint.h>
#include <stdbool.h>

#ifndef DELAY
#define DELAY 2000000
#endif

typedef enum { MODE_IDLE = 0, MODE_RUN = 3, MODE_FAULT = 7 } mode_e;
typedef struct { uint8_t a : 3; uint8_t b : 5; uint16_t c; } bits_t;
typedef union { uint32_t word; uint8_t bytes[4]; } word_u;
typedef struct {
    int32_t temperature_mc;
    float gain;
    mode_e mode;
    bits_t flags;
    uint16_t samples[4];
    word_u raw;
    const char *name;
    struct { int8_t x, y; } pos;
} state_t;

volatile uint32_t g_ticks;
volatile uint32_t g_watched;
state_t g_state = { -1234, 1.5f, MODE_RUN, { 5, 17, 0xBEEF }, { 1, 2, 3, 4 }, { 0x11223344u }, "probe", { -3, 7 } };
static volatile uint32_t s_count;
char g_label[12] = "debug";
#ifdef HANG
volatile bool g_hang = true;
#else
volatile bool g_hang;
#endif

__attribute__((noinline)) void writer(uint32_t v) {
    static uint32_t s_count;   /* shares its name with the file's s_count: a read by name refuses it */
    s_count++;
    g_watched = v;
}
__attribute__((noinline)) void reached_tick_five(void) { __asm volatile ("nop"); }
__attribute__((noinline)) void level3(uint32_t depth) { volatile uint32_t spin = depth; while (g_hang) { spin++; } }
__attribute__((noinline)) void level2(uint32_t depth) { uint32_t local2 = depth * 10u; level3(local2 + 1u); }
__attribute__((noinline)) void level1(uint32_t depth) { level2(depth + 1u); }

extern uint32_t _sbss, _ebss, _estack;
static void delay(uint32_t n) { for (volatile uint32_t i = 0; i < n; i++) {} }

void reset_handler(void) {
    for (uint32_t *p = &_sbss; p < &_ebss; p++) *p = 0;
    for (;;) {
        g_ticks++;
        s_count += 3u;
        writer(g_ticks * 2u);
        if (g_ticks == 5u) reached_tick_five();
        g_state.samples[g_ticks & 3u]++;
        level1(0);
        delay(DELAY);
    }
}

void default_handler(void) { for (;;) {} }

__attribute__((section(".isr_vector"), used))
void (*const vectors[16])(void) = {
    (void (*)(void))&_estack, reset_handler,
    default_handler, default_handler, default_handler, default_handler, default_handler,
    0, 0, 0, 0, default_handler, default_handler, 0, default_handler, default_handler,
};
