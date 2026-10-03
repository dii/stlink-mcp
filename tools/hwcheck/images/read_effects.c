/* A program that leaves peripherals holding data and never touches them again,
   so that a debugger's read is the only thing that could change their flags.
   For tools/hwcheck and the measurements behind read_effects.py.

   Nothing outside the chip is driven. The SPI is master and clocks in whatever
   its unconnected input reads; the ADC converts an internal channel or an
   unconnected pin. On the F407, UART5 listens on PD2 for a byte sent from the
   host through a serial adapter; the pin is an input. On the H7 the UART
   measured is the ST-LINK's virtual COM port, with tools/hwcheck's h7_ram, so
   it is not here. On the H7 the RTC is started from the LSI if no clock was
   chosen for it, which lasts until power is removed.

   STATUS_ADDR reads 0x5EAD0000, the number of the block being set up in bits
   8 to 15, and one bit per block that reached its flag; COUNTER_ADDR counts,
   as in beacon.c. */
#include <stdint.h>
#define REG(a) (*(volatile uint32_t *)(a))

#define WAIT_FOR(cond) do { for (uint32_t i_ = 0; i_ < 200000u; i_++) if (cond) return 1u; return 0u; } while (0)

static void delay(uint32_t n) { for (volatile uint32_t i = 0; i < n; i++) {} }

#if defined(H7)
#define COUNTER_ADDR 0x24070000u
#define STATUS_ADDR  0x24070004u

static uint32_t setup_usart(void) { return 0; }   /* the VCP, with h7_ram */

/* SPI4, master, one 8-bit frame. Kernel clock rcc_pclk2 at reset. */
static uint32_t setup_spi(void) {
    uint32_t sel = REG(0x58024450);             /* RCC_D2CCIP1R.SPI45SEL = 3: hsi_ker_ck */
    sel &= ~(7u << 16); sel |= 3u << 16;
    REG(0x58024450) = sel;
    REG(0x580244F0) |= 1u << 13;                /* RCC_APB2ENR.SPI4EN */
    (void)REG(0x580244F0);
    REG(0x40013400) = 1u << 12;                 /* CR1.SSI first: MASTER with SS low is a mode fault */
    REG(0x40013408) = (7u << 0) | (7u << 28);   /* CFG1: DSIZE 8 bits, MBR /256 */
    REG(0x4001340C) = (1u << 22) | (1u << 26);  /* CFG2: MASTER, SSM */
    REG(0x40013404) = 1;                        /* CR2.TSIZE: one frame */
    REG(0x40013400) = (1u << 12) | 1u;          /* CR1: SSI, SPE */
    *(volatile uint8_t *)0x40013420 = 0xA5;     /* TXDR */
    REG(0x40013400) = (1u << 12) | 1u | (1u << 9);   /* CSTART */
    WAIT_FOR(REG(0x40013414) & 1u);             /* SR.RXP */
}

/* ADC3, channel 0, one conversion. Clock hclk/4, 16 MHz. */
static uint32_t setup_adc(void) {
    REG(0x580244E0) |= 1u << 24;                /* RCC_AHB4ENR.ADC3EN */
    (void)REG(0x580244E0);
    REG(0x58026308) = 3u << 16;                 /* ADC3 CCR.CKMODE: hclk/4 */
    REG(0x58026008) = 0;                        /* CR: out of deep power-down */
    REG(0x58026008) = 1u << 28;                 /* ADVREGEN */
    delay(50000);
    REG(0x58026008) = (1u << 28) | (2u << 8);   /* BOOST for 12.5 to 25 MHz (revision V) */
    REG(0x58026000) = 1u;                       /* ISR: clear ADRDY */
    REG(0x58026008) |= 1u;                      /* ADEN */
    for (uint32_t i = 0; i < 200000u && !(REG(0x58026000) & 1u); i++) {}
    REG(0x5802601C) = 1u;                       /* PCSEL: channel 0 */
    REG(0x58026014) = 7u;                       /* SMPR1: channel 0, longest sampling time */
    REG(0x58026030) = 0;                        /* SQR1: one conversion, channel 0 */
    REG(0x58026008) |= 1u << 2;                 /* ADSTART */
    WAIT_FOR(REG(0x58026000) & (1u << 2));      /* ISR.EOC */
}

/* RTC from the LSI, if nothing was chosen for it; RTCSEL is set once per
   backup-domain reset, so a clock already chosen is left as it is. */
static uint32_t setup_rtc(void) {
    REG(0x580244F4) |= 1u << 16;                /* RCC_APB4ENR.RTCAPBEN */
    REG(0x58024800) |= 1u << 8;                 /* PWR_CR1.DBP */
    REG(0x58024474) |= 1u;                      /* RCC_CSR.LSION */
    for (uint32_t i = 0; i < 200000u && !(REG(0x58024474) & 2u); i++) {}
    uint32_t bdcr = REG(0x58024470);
    if (((bdcr >> 8) & 3u) == 0) bdcr |= 2u << 8;   /* RTCSEL: LSI */
    REG(0x58024470) = bdcr | (1u << 15);        /* RTCEN */
    WAIT_FOR(REG(0x58004000) != 0 || REG(0x58004008) != 0);   /* TR or SSR moving */
}

#elif defined(F4)
#define COUNTER_ADDR 0x2001F000u
#define STATUS_ADDR  0x2001F004u

/* UART5 receiving on PD2, AF8, an input, from the 16 MHz HSI. The byte comes
   from the host; this only sets the block up. */
static uint32_t setup_usart(void) {
    REG(0x40023830) |= 1u << 3;                 /* RCC_AHB1ENR.GPIODEN */
    REG(0x40023840) |= 1u << 20;                /* RCC_APB1ENR.UART5EN */
    (void)REG(0x40023840);
    uint32_t af = REG(0x40020C20);              /* GPIOD_AFRL: AF8 on PD2 */
    af &= ~(0xFu << 8); af |= 8u << 8;
    REG(0x40020C20) = af;
    uint32_t m = REG(0x40020C00);               /* GPIOD_MODER: PD2 alternate */
    m &= ~(3u << 4); m |= 2u << 4;
    REG(0x40020C00) = m;
    REG(0x40005008) = 139;                      /* BRR: 16 MHz / 115200 */
    REG(0x4000500C) = (1u << 13) | (1u << 2);   /* CR1: UE, RE */
    return 1u;
}

/* SPI1, master, one 8-bit frame. */
static uint32_t setup_spi(void) {
    REG(0x40023844) |= 1u << 12;                /* RCC_APB2ENR.SPI1EN */
    (void)REG(0x40023844);
    REG(0x40013000) = (1u << 2) | (7u << 3) | (1u << 8) | (1u << 9);   /* CR1: MSTR, BR /256, SSI, SSM */
    REG(0x40013000) |= 1u << 6;                 /* SPE */
    REG(0x4001300C) = 0xA5;                     /* DR */
    WAIT_FOR(REG(0x40013008) & 1u);             /* SR.RXNE */
}

/* ADC1, the internal reference on channel 17, one conversion. */
static uint32_t setup_adc(void) {
    REG(0x40023844) |= 1u << 8;                 /* RCC_APB2ENR.ADC1EN */
    (void)REG(0x40023844);
    REG(0x40012304) |= 1u << 23;                /* CCR.TSVREFE: VREFINT on channel 17 */
    REG(0x4001200C) = 7u << 21;                 /* SMPR1: channel 17, 480 cycles */
    REG(0x40012034) = 17;                       /* SQR3: SQ1 = 17 */
    REG(0x40012008) = 1u;                       /* CR2.ADON */
    delay(10000);
    REG(0x40012008) = 1u | (1u << 30);          /* SWSTART */
    WAIT_FOR(REG(0x40012000) & (1u << 1));      /* SR.EOC */
}

static uint32_t setup_rtc(void) { return 0; }   /* not touched: see the header */
#else
#error H7 or F4
#endif

void reset_handler(void) {
    uint32_t status = 0;
    uint32_t (*const steps[])(void) = { setup_usart, setup_spi, setup_adc, setup_rtc };
    for (uint32_t i = 0; i < 4; i++) {
        REG(STATUS_ADDR) = 0x5EAD0000u | (i << 8) | status;
        status |= steps[i]() << i;
    }
    REG(STATUS_ADDR) = 0x5EAD0000u | (4u << 8) | status;
    for (uint32_t tick = 0;; tick++) {
        REG(COUNTER_ADDR) = 0xC0FFEE00u + tick;
        delay(1000000);
    }
}

__attribute__((naked)) void fault_handler(void) { __asm volatile("b ."); }

extern uint32_t _estack;
__attribute__((section(".isr_vector"), used)) void (*const vectors[16])(void) = {
    (void (*)(void))&_estack, reset_handler,
    fault_handler, fault_handler, fault_handler, fault_handler, fault_handler,
    fault_handler, 0, 0, 0, fault_handler, fault_handler, 0, fault_handler, fault_handler,
};
