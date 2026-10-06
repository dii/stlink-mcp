/* Test images for tools/hwcheck. Each variant is selected by -D flags in
   build_images.py. No start-up code, no .data or .bss: everything the image
   needs is set in code, so it runs the same whether a debugger loaded it into
   RAM or the boot ROM started it from flash.

   Every image writes 0xC0FFEE00 plus a tick count to COUNTER_ADDR, so a
   debugger can see it running by reading that word twice. With HAS_UART it also
   prints "stlink-mcp hw check <TAG> tick <n>" on the board's virtual COM port. */
#include <stdint.h>
#define REG(a) (*(volatile uint32_t *)(a))

#ifndef COUNTER_ADDR
#error COUNTER_ADDR
#endif
#ifndef TAG
#define TAG "ram"
#endif
#ifndef DELAY
#define DELAY 3000000
#endif

#ifdef F4_UART5   /* STM32F407: UART5 TX on PC12, AF8, at 115200 from the 16 MHz reset clock.
                     That is where the board this was measured on has its console; on
                     other boards the image still runs, and its counter is what shows it. */
static void uart_init(void) {
    REG(0x40023830) |= 1u << 2;                 /* RCC_AHB1ENR.GPIOCEN */
    REG(0x40023840) |= 1u << 20;                /* RCC_APB1ENR.UART5EN */
    (void)REG(0x40023840);
    uint32_t m = REG(0x40020800);               /* GPIOC_MODER: PC12 alternate */
    m &= ~(3u << 24); m |= 2u << 24;
    REG(0x40020800) = m;
    uint32_t af = REG(0x40020824);              /* GPIOC_AFRH: AF8 on PC12 */
    af &= ~(0xFu << 16); af |= 8u << 16;
    REG(0x40020824) = af;
    REG(0x40005008) = 139;                      /* UART5_BRR: 16 MHz HSI / 115200 */
    REG(0x4000500C) = (1u << 13) | (1u << 3);   /* CR1: UE, TE */
}
static void uart_putc(char c) { while (!(REG(0x40005000) & (1u << 7))) {} REG(0x40005004) = (uint8_t)c; }
#elif defined(N6_UART)   /* NUCLEO-N657X0-Q: the ST-LINK VCP is USART1, PE5 TX, AF7 (ST's
                            Nucleo BSP, COM1). Secure aliases, since the image runs Secure. */
static void uart_init(void) {
    REG(0x56028A5C) = 1u << 4;                  /* RCC_AHB4ENSR: GPIOE */
    REG(0x56028A6C) = 1u << 4;                  /* RCC_APB2ENSR: USART1 */
    (void)REG(0x56028A6C);
    uint32_t sel = REG(0x56028174);             /* RCC_CCIPR13.USART1SEL = 6: hsi_div_ck, */
    sel &= ~7u; sel |= 6u;                      /* 64 MHz with HSIDIV at its reset value */
    REG(0x56028174) = sel;
    uint32_t m = REG(0x56021000);               /* GPIOE_MODER: PE5 alternate */
    m &= ~(3u << 10); m |= 2u << 10;
    REG(0x56021000) = m;
    uint32_t af = REG(0x56021020);              /* GPIOE_AFRL: AF7 on PE5 */
    af &= ~(0xFu << 20); af |= 7u << 20;
    REG(0x56021020) = af;
    REG(0x5200100C) = 556;                      /* USART1_BRR: 64 MHz / 115200 */
    REG(0x52001000) = (1u << 0) | (1u << 3);    /* CR1: UE, TE; FIFO off */
}
static void uart_putc(char c) { while (!(REG(0x5200101C) & (1u << 7))) {} REG(0x52001028) = (uint8_t)c; }
#elif defined(HAS_UART)   /* NUCLEO-144 STM32H7: the ST-LINK VCP is USART3, PD8 TX / PD9 RX, AF7 */
#ifndef BRR
#define BRR 556   /* 64 MHz HSI / 115200 */
#endif
static void uart_init(void) {
    /* A reset taken while earlier firmware clocked USART3 from PLL3 leaves its
       kernel clock without a clock until PLL3 runs again (measured 2026-10-01,
       docs/findings.md). Start PLL3 so the clock selector can switch. */
    REG(0x58024400) |= 1u << 28;                /* RCC_CR.PLL3ON */
    while (!(REG(0x58024400) & (1u << 29))) {}  /* PLL3RDY */
    REG(0x580244E0) |= 1u << 3;                 /* RCC_AHB4ENR.GPIODEN */
    REG(0x580244E8) |= 1u << 18;                /* RCC_APB1LENR.USART3EN */
    (void)REG(0x580244E8);
    uint32_t m = REG(0x58020C00);               /* GPIOD_MODER: PD8, PD9 alternate */
    m &= ~((3u << 16) | (3u << 18)); m |= (2u << 16) | (2u << 18);
    REG(0x58020C00) = m;
    uint32_t af = REG(0x58020C24);              /* GPIOD_AFRH: AF7 on PD8, PD9 */
    af &= ~0xFFu; af |= 0x77u;
    REG(0x58020C24) = af;
    REG(0x4000480C) = BRR;
    REG(0x40004800) = (1u << 0) | (1u << 2) | (1u << 3);  /* UE, RE, TE */
}
static void uart_putc(char c) { while (!(REG(0x4000481C) & (1u << 7))) {} REG(0x40004828) = (uint8_t)c; }
#else
static void uart_init(void) {}
static void uart_putc(char c) { (void)c; }
#endif

#ifdef ITM_OUT   /* each character also goes to ITM stimulus port 0, for SWO, but only
                    once a debugger has enabled ITM and that port; until then nothing */
static void itm_putc(char c) {
    if ((REG(0xE0000E80) & 1u) && (REG(0xE0000E00) & 1u)) {   /* ITM_TCR.ITMENA, ITM_TER bit 0 */
        while (REG(0xE0000000) == 0) {}                        /* port 0 FIFO ready */
        *(volatile uint8_t *)0xE0000000 = (uint8_t)c;
    }
}
#else
static void itm_putc(char c) { (void)c; }
#endif

static void putc_(char c) { uart_putc(c); itm_putc(c); }
static void puts_(const char *s) { while (*s) putc_(*s++); }
static void putu(uint32_t v) { char b[11]; int i = 10; b[i] = 0; do { b[--i] = '0' + v % 10; v /= 10; } while (v); puts_(&b[i]); }

static void delay(uint32_t n) { for (volatile uint32_t i = 0; i < n; i++) {} }

#ifdef BOOT_REPORT  /* STM32N6: the boot and debug state as this image sees it, printed so a
                       console shows it even when the debug port stays closed. RM0486 4.5. */
static void puthex(uint32_t v) {
    puts_("0x");
    for (int i = 28; i >= 0; i -= 4) putc_("0123456789ABCDEF"[(v >> i) & 0xF]);
}
static void boot_report(uint32_t clock_at_entry) {
    puts_("stlink-mcp hw check " TAG " bsec_sr "); puthex(REG(0x56009E40));  /* NVSTATE, 31:26 */
    puts_(" apb4h_at_entry "); puthex(clock_at_entry);                        /* bit 0: SYSCFG clock */
    puts_(" hdpl "); puthex(REG(0x56009E94));                                  /* BSEC_HDPLSR */
    puts_(" dbgcr "); puthex(REG(0x56009E8C));                                 /* BSEC_DBGCR */
    puts_(" ap_unlock "); puthex(REG(0x56009E90));                             /* BSEC_AP_UNLOCK */
    puts_(" boot_pins "); puthex(REG(0x56008100));                             /* SYSCFG_BOOTSR */
    puts_("\r\n");
}
#endif

extern void (*const vectors[16])(void);

#ifdef CLOSE_SWD  /* PA13 and PA14, SWDIO and SWCLK on these parts, made plain inputs:
                     the debug port closes as soon as this runs, and drives nothing */
static void close_swd(void) {
#ifdef F4_UART5
    REG(0x40023830) |= 1u;                      /* RCC_AHB1ENR.GPIOAEN */
    REG(0x40020000) &= ~(0xFu << 26);           /* GPIOA_MODER: PA13, PA14 input */
#else
    REG(0x580244E0) |= 1u;                      /* RCC_AHB4ENR.GPIOAEN */
    REG(0x58020000) &= ~(0xFu << 26);           /* GPIOA_MODER: PA13, PA14 input */
#endif
}
#endif

void reset_handler(void) {
#ifdef OPEN_DEBUG  /* STM32N6 booted from flash: the boot ROM leaves debug closed, and only
                      secure privileged code after it can open it (RM0486 3.9, 4.3.11, 4.5.18
                      and 4.5.19). Both registers can be written once per reset, and part of
                      BSEC_AP_UNLOCK clears at every warm reset, so every boot writes both.
                      For development builds only: it undoes the part's protection. */
    REG(0x56009E8C) = 0xB451B400u;  /* BSEC_DBGCR: AUTH_SEC 0xB4 (secure debug too), AUTH_HDPL
                                       0x51 (from boot level 1), UNLOCK 0xB4 */
    REG(0x56009E90) = 0xB4u;        /* BSEC_AP_UNLOCK: open access port 1, the core's */
#endif
#ifdef RESET_XSPI  /* STM32N6: put the flash controllers back as a power-up leaves them, since
                      the boot ROM's flash boot leaves them configured and ST's external loader
                      then fails to erase or read. RCC_AHB5RSTSR/RSTCR: XSPIM, XSPI2, XSPI1. */
    REG(0x56028A20) = (1u << 13) | (1u << 12) | (1u << 5);
    REG(0x56029220) = (1u << 13) | (1u << 12) | (1u << 5);
#endif
#ifdef BOOT_REPORT  /* the SYSCFG clock as the boot ROM left it, then on: the boot-pin register
                       needs it, and debug access port 1 closes without it (docs/findings.md,
                       2026-08-16). Every BOOT_REPORT image does this, so the images with and
                       without OPEN_DEBUG differ in the BSEC writes alone. */
    uint32_t clock_at_entry = REG(0x56028278);  /* RCC_APB4HENR */
    REG(0x56028A78) = 1u;                       /* RCC_APB4HENSR: SYSCFGENS */
#endif
#ifdef CLOSE_AP1  /* STM32N6: what the end of ST's stock SystemInit does, RCC_APB4ENCR2 =
                     SYSCFGENC. Without the SYSCFG clock, debug access port 1 closes until
                     power is removed (docs/findings.md, 2026-08-16). */
    REG(0x56029278) = 1u;
#endif
#ifdef SET_VTOR
    REG(0xE000ED08) = (uint32_t)vectors;        /* SCB->VTOR: our own table, so our handler runs */
#endif
    uart_init();
    for (uint32_t tick = 0;; tick++) {
        REG(COUNTER_ADDR) = 0xC0FFEE00u + tick;
#ifdef BOOT_REPORT
        if ((tick & 7) == 0) boot_report(clock_at_entry);
#endif
        puts_("stlink-mcp hw check " TAG " tick "); putu(tick); puts_("\r\n");
#ifdef CLOSE_SWD
        if (tick == 2) close_swd();
#endif
#ifdef SLEEP_FOREVER  /* no interrupt is enabled, so nothing wakes it */
        if (tick == 1) for (;;) __asm volatile("wfi");
#endif
#ifdef FAULT_AFTER
        if (tick == FAULT_AFTER) {
#ifdef USE_PSP   /* run on PSP from here, as an RTOS thread does; same address, so locals survive */
            __asm volatile("mrs r0, msp\n msr psp, r0\n mrs r0, control\n orr r0, r0, #2\n msr control, r0\n isb" ::: "r0", "memory");
#endif
#ifdef USE_FP    /* make FP context active, so the exception frame is the extended one */
            REG(0xE000ED88) |= 0xFu << 20;      /* CPACR: CP10, CP11 full access */
            __asm volatile("dsb\n isb");
            __asm volatile("vmov s0, %0\n vadd.f32 s0, s0, s0" :: "r"(tick) : "s0");
#endif
#ifdef BUS_FAULT /* a buffered store to an address nothing answers: an imprecise bus error */
            puts_("faulting now: store to 0x2C000000\r\n");
            *(volatile uint32_t *)0x2C000000u = 0xDEADBEEFu;
            for (volatile int spin = 0; spin < 100; spin++) {}
#else
            puts_("faulting now: udf\r\n");
            __asm volatile("udf #0");
#endif
        }
#endif
        delay(DELAY);
    }
}

#ifdef LOCKUP  /* a fault handler that faults: the core locks up */
__attribute__((naked)) void fault_handler(void) { __asm volatile("udf #1"); }
#elif defined(CALLING_HANDLER)  /* a handler that calls a function first, which overwrites LR */
__attribute__((noinline)) void handler_helper(void) { __asm volatile("nop"); }
void fault_handler(void) { handler_helper(); for (;;) {} }
#else
__attribute__((naked)) void fault_handler(void) { __asm volatile("b ."); }
#endif

extern uint32_t _estack;
__attribute__((section(".isr_vector"), used)) void (*const vectors[16])(void) = {
    (void (*)(void))&_estack, reset_handler,
    fault_handler, fault_handler, fault_handler, fault_handler, fault_handler,  /* NMI, HardFault, MemManage, BusFault, UsageFault */
    fault_handler, 0, 0, 0, fault_handler, fault_handler, 0, fault_handler, fault_handler,
};

#ifdef EXTRA_SEGMENT  /* a second loadable segment, placed by the linker script */
__attribute__((section(".extra"), used)) const uint32_t extra_segment[4] = {1, 2, 3, 4};
#endif
