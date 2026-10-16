/*
 * ICI 9320P "explorer" firmware for the camera's Cypress FX2LP.
 *
 * Purpose: discover how the thermal sensor core is wired to the FX2 without
 * ICI's firmware. Everything is driven from the host through EP0 vendor
 * requests (see tools/explorer.py). The firmware never drives a port pin
 * unless the host explicitly asks for it (PORT_WRITE / OE_WRITE).
 *
 * Vendor requests (bmRequestType 0xC0 = IN, 0x40 = OUT):
 *   0x10 READ_PINS      IN  8   IOA IOB IOC IOD IOE GPIFREADYSTAT IFCONFIG GPIFIDLECTL
 *   0x11 ACTIVITY       IN  48  wValue = sample iterations; per-pin transition
 *                               counts (saturating u8): A0-7 B0-7 C0-7 D0-7 E0-7 RDY0-5, pad, pad
 *   0x12 I2C_SCAN       IN  16  bitmap of 7-bit addresses that ACK a write
 *   0x13 I2C_READ       IN  n   wValue = addr7, wIndex = reg | 0x100 (no reg write if bit 8 set)
 *   0x14 I2C_WRITE      OUT n   wValue = addr7, payload = bytes (first is usually reg)
 *   0x15 PORT_WRITE     OUT 0   wValue = port index (0..4 = A..E), wIndex = value
 *   0x16 OE_WRITE       OUT 0   wValue = port index, wIndex = OE mask (1 = output!)
 *   0x18 SET_CTL        OUT 0   wValue = CTL0..5 levels used by GPIF waveforms + idle (default: GPIFIDLECTL)
 *   0x17 RDY_TRACE      IN  64  run-length trace of (GPIFREADYSTAT & wValue) sampled ~1 us apart
 *                               for wIndex iterations: up to 21 x (level, runlen16) + count16
 *   0x20 UART_SETUP     OUT 0   wValue:wIndex = baud (wIndex high word) -> sio0 at that baud
 *   0x21 UART_TX        OUT n   payload bytes sent on TXD0
 *   0x22 UART_RX        IN  n   returns up to n bytes from the RX ring buffer
 *   0x30 XREG_READ      IN  n   wValue = xdata address
 *   0x31 XREG_WRITE     OUT n   wValue = xdata address
 *   0x40 STREAM_START   OUT 0   wValue = mode (0 slave FIFO, 1 GPIF gated on RDYn, 2 GPIF free-run,
 *                               3 GPIF one sample per RDYn pulse, 4 one frame (RDY0 valid, RDY1 strobe),
 *                               5 continuous frames)
 *                               wIndex flags: b0 16-bit bus, b1 internal IFCLK, b2 48 MHz,
 *                               b3 IFCLK polarity, b4 drive IFCLK out, b5 active-high FIFO pins,
 *                               b8..b10 RDY index for modes 1/3, b11 pause while EP6 FIFO full,
 *                               b12 synchronous RDY sampling, b13 sample on strobe low phase
 *   0x41 STREAM_STOP    OUT 0
 *   0x42 STREAM_STATUS  IN  12  IFCONFIG GPIFTRIG GPIFIDLECS GPIFREADYSTAT EP6CS EP2468STAT
 *                               EP6FIFOFLGS EP6FIFOBCH EP6FIFOBCL FIFOPINPOLAR EP6FIFOCFG REVCTL
 *   0x43 COMMIT_TAIL    IN  2   commit whatever is in the EP6 FIFO as a short packet; returns byte count
 *   0x50 VERSION        IN  4   'X','P', major, minor
 *   0xA0 (Cypress loader protocol, handled by the firmware itself since RENUM=1):
 *        writing CPUCS(0xE600)=1 enters a resident loader; the host then uploads a
 *        new image with 0xA0 writes and restarts with CPUCS=0. No replug needed.
 * Endpoints: EP2 IN (double), EP4 OUT (double), EP6 IN (quad, video) bulk 512, so a
 * slave-FIFO master can write to FIFOADR 00 (EP2) or 10 (EP6) and the host reads it.
 */
#include <fx2regs.h>
#include <fx2macros.h>
#include <delay.h>
#include <autovector.h>
#include <setupdat.h>
#include <gpif.h>
#include <serial.h>
#include <i2c.h>

#define SYNCDELAY SYNCDELAY4
#include <eputils.h>

#define VR_READ_PINS     0x10
#define VR_ACTIVITY      0x11
#define VR_I2C_SCAN      0x12
#define VR_I2C_READ      0x13
#define VR_I2C_WRITE     0x14
#define VR_PORT_WRITE    0x15
#define VR_OE_WRITE      0x16
#define VR_RDY_TRACE     0x17
#define VR_SET_CTL       0x18
#define VR_UART_SETUP    0x20
#define VR_UART_TX       0x21
#define VR_UART_RX       0x22
#define VR_XREG_READ     0x30
#define VR_XREG_WRITE    0x31
#define VR_STREAM_START  0x40
#define VR_STREAM_STOP   0x41
#define VR_STREAM_STATUS 0x42
#define VR_COMMIT_TAIL   0x43
#define VR_VERSION       0x50
#ifndef bmGPIFWF
#define bmGPIFWF 0x02          /* GPIFIRQ/GPIFIE bit 1: waveform GINT */
#endif

volatile __bit got_sud;
volatile __bit frame_mark;        /* modes 4/5: commit a short packet at each frame boundary */
__xdata WORD frame_count;
__xdata BYTE ctl_levels;
__xdata BYTE ctl_override;

/* ------------------------------------------------------------ UART ring */
#define RXBUF_SIZE 128
__xdata BYTE rxbuf[RXBUF_SIZE];
volatile BYTE rx_head, rx_tail;
volatile __bit tx_idle;
volatile __bit uart_on;

void serial0_isr(void) __interrupt (4)
{
    if (RI) {
        RI = 0;
        rxbuf[rx_head] = SBUF0;
        rx_head = (rx_head + 1) & (RXBUF_SIZE - 1);
    }
    if (TI) {
        TI = 0;
        tx_idle = 1;
    }
}

static void uart_setup(DWORD baud)
{
    ES0 = 0;
    sio0_init(baud);      /* sets TI=1 as "ready" marker */
    rx_head = rx_tail = 0;
    tx_idle = 1;
    TI = 0;
    ES0 = 1;
    uart_on = 1;
}

static void uart_tx(BYTE c)
{
    WORD guard = 0;
    while (!tx_idle && ++guard) ;
    tx_idle = 0;
    SBUF0 = c;
}

/* ------------------------------------------------------------ EP0 helpers */
__xdata BYTE scratch[64];

static void ep0_send(BYTE n)
{
    EP0BCH = 0;
    SYNCDELAY;
    EP0BCL = n;
}

/* read a host->device data stage into scratch (max 64 bytes) */
static BYTE ep0_recv(void)
{
    BYTE n, i;
    EP0BCH = 0;
    SYNCDELAY;
    EP0BCL = 0;          /* arm */
    while (EP0CS & bmEPBUSY) ;
    n = EP0BCL;
    for (i = 0; i < n; ++i) scratch[i] = EP0BUF[i];
    return n;
}

/* ------------------------------------------------------------ I2C probe */
static BOOL i2c_probe(BYTE addr7)
{
    WORD t;
    BYTE s = 0;
    I2CS |= bmSTART;
    I2DAT = addr7 << 1;
    for (t = 0; t < 20000; ++t) {
        s = I2CS;
        if (s & bmDONE) break;
    }
    I2CS |= bmSTOP;
    for (t = 0; t < 20000 && (I2CS & bmSTOP); ++t) ;
    if (!(s & bmDONE) || (s & bmBERR)) return FALSE;
    return (s & bmACK) ? TRUE : FALSE;
}

/* ------------------------------------------------------------ activity */
static void count_bits(BYTE x, __xdata BYTE *cnt)
{
    BYTE i, m = 1;
    for (i = 0; i < 8; ++i, m <<= 1)
        if ((x & m) && cnt[i] != 255) cnt[i]++;
}

static void sample_activity(WORD iters)
{
    BYTE la, lb, lc, ld, le, lr, v;
    WORD n;
    BYTE i;
    for (i = 0; i < 48; ++i) scratch[i] = 0;
    la = IOA; lb = IOB; lc = IOC; ld = IOD; le = IOE; lr = GPIFREADYSTAT;
    for (n = 0; n < iters; ++n) {
        v = IOA; count_bits(v ^ la, scratch + 0);  la = v;
        v = IOB; count_bits(v ^ lb, scratch + 8);  lb = v;
        v = IOC; count_bits(v ^ lc, scratch + 16); lc = v;
        v = IOD; count_bits(v ^ ld, scratch + 24); ld = v;
        v = IOE; count_bits(v ^ le, scratch + 32); le = v;
        v = GPIFREADYSTAT; count_bits((v ^ lr) & 0x3F, scratch + 40); lr = v;
    }
}

/* ------------------------------------------------------------ RDY trace */
static void rdy_trace(BYTE mask, WORD iters)
{
    BYTE last, v, nrun = 0, i;
    WORD run = 0, n;
    for (i = 0; i < 64; ++i) scratch[i] = 0;
    last = GPIFREADYSTAT & mask;
    for (n = 0; n < iters; ++n) {
        v = GPIFREADYSTAT & mask;
        if (v != last) {
            if (nrun < 21) {
                scratch[nrun * 3] = last;
                scratch[nrun * 3 + 1] = LSB(run);
                scratch[nrun * 3 + 2] = MSB(run);
                ++nrun;
            }
            last = v;
            run = 0;
        }
        ++run;
    }
    if (nrun < 21) {
        scratch[nrun * 3] = last; scratch[nrun * 3 + 1] = LSB(run); scratch[nrun * 3 + 2] = MSB(run); ++nrun;
    }
    scratch[63] = nrun;
}

/* ------------------------------------------------------------ streaming */
static void fifo_reset_all(void)
{
    FIFORESET = 0x80; SYNCDELAY;
    FIFORESET = 0x02; SYNCDELAY;
    FIFORESET = 0x04; SYNCDELAY;
    FIFORESET = 0x06; SYNCDELAY;
    FIFORESET = 0x08; SYNCDELAY;
    FIFORESET = 0x00; SYNCDELAY;
}

static void stream_stop(void)
{
    frame_mark = 0;
    GPIFABORT = 0xFF; SYNCDELAY;
    IFCONFIG = 0x80; SYNCDELAY;          /* ports mode, internal IFCLK (not output), nothing driven */
    EP2FIFOCFG = 0x00; SYNCDELAY;
    EP4FIFOCFG = 0x00; SYNCDELAY;
    EP6FIFOCFG = 0x00; SYNCDELAY;
    EP8FIFOCFG = 0x00; SYNCDELAY;
    fifo_reset_all();
    EP4BCL = 0x80; SYNCDELAY;
    EP4BCL = 0x80; SYNCDELAY;
}

/* Build GPIF waveform 0 (used for FIFO reads).
 * mode 1: S0 waits for RDYn high, S1 samples every IFCLK while RDYn high.
 * mode 2: S0 samples every IFCLK unconditionally (INTRDY term).           */
static void build_waveform(BYTE mode, BYTE rdy, BYTE gate_full, BYTE inv)
{
    BYTE i, idle_ctl, cond, lfn, strobe;
    __xdata BYTE *w = &GPIF_WAVE_DATA;
    idle_ctl = ctl_override ? (ctl_levels & 0x3F) : (GPIFIDLECTL & 0x3F);
    if (ctl_override) { GPIFIDLECTL = idle_ctl; SYNCDELAY; }
    EP6GPIFFLGSEL = 0x02; SYNCDELAY;            /* FIFO-flag term = EP6 FULL flag */
    for (i = 0; i < 128; ++i) w[i] = 0;
    /* LENBR[0..7] OPCODE[8..15] OUTPUT[16..23] LFN[24..31] */
    for (i = 0; i < 8; ++i) { w[i] = 1; w[16 + i] = idle_ctl; }
    if (mode == 2) {
        GPIFREADYCFG |= 0x80;                   /* INTRDY = 1 -> always-true term */
        cond = 7;
    } else {
        GPIFREADYCFG &= ~0x80;
        cond = rdy & 7;
    }
    /* cond AND cond, or (/FULL AND cond) when gating on the FIFO flag */
    lfn = gate_full ? ((3 << 6) | (cond << 3) | 6) : ((0 << 6) | (cond << 3) | cond);
    if (mode == 4 || mode == 5) {
        /* frame-aligned pixel capture. RDY0 = frame signal, RDY1 = pixel strobe.
         * S0: wait RDY0 low                         RDY0 ? S0 : S1
         * S1: wait RDY0 high                        RDY0 ? S2 : S1
         * S2: wait strobe (or frame end)            RDY1 ? S3 : S4
         * S3: DATA on entry; hold while RDY1 high   RDY1 ? S3 (no re-execute) : S2
         * S4: frame still valid?                    RDY0 ? S2 : (S7 done | S1 next frame)   */
        w[0] = (0 << 3) | 1; w[8]  = 0x01; w[24] = (0 << 6) | (0 << 3) | 0;
        w[1] = (2 << 3) | 1; w[9]  = 0x11; w[25] = (0 << 6) | (0 << 3) | 0;   /* GINT: flag frame boundary */
        /* strobe term: RDY1 AND RDY1, or (/RDY1 AND RDY0) to use the strobe's low phase */
        strobe = inv ? ((3 << 6) | (0 << 3) | 1) : ((0 << 6) | (1 << 3) | 1);
        w[2] = (3 << 3) | 4; w[10] = 0x01; w[26] = strobe;
        w[3] = (3 << 3) | 2; w[11] = 0x03; w[27] = strobe;
        /* NOTE: in FIFO mode the GPIF restarts the waveform at S0 after reaching S7 until the
         * transaction count expires, so a single frame must park in a looping state (S6). */
        w[4] = (2 << 3) | (mode == 4 ? 6 : 1); w[12] = 0x01; w[28] = (0 << 6) | (0 << 3) | 0;
        w[6] = (6 << 3) | 6; w[14] = 0x01; w[30] = (0 << 6) | (0 << 3) | 0;   /* S6: park forever */
    } else if (mode == 3) {
        /* one sample per RDYn pulse, 2 states:
         * S0: wait RDYn high -> S1;  S1: DATA on entry, hold while RDYn high (no re-exec), -> S0 */
        strobe = inv ? ((3 << 6) | (0 << 3) | cond) : ((0 << 6) | (cond << 3) | cond);
        w[0]  = (1 << 3) | 0;  w[8]  = 0x01; w[24] = strobe;
        w[1]  = (1 << 3) | 0;  w[9]  = 0x03; w[25] = strobe;
    } else {
        /* S0: decision, no data: cond ? S1 : S0 */
        w[0]  = (1 << 3) | 0;  w[8]  = 0x01; w[24] = lfn;
        /* S1: decision + data, re-execute every IFCLK while cond, else back to S0 */
        w[1]  = 0x80 | (1 << 3) | 0;  w[9]  = 0x03; w[25] = lfn;
    }
}

static void stream_start(BYTE mode, WORD flags)
{
    BYTE ifc = 0, ww;
    stream_stop();
    ww = (flags & 1) ? bmWORDWIDE : 0;
    REVCTL = 0x03; SYNCDELAY;
    EP2AUTOINLENH = 0x02; SYNCDELAY;
    EP2AUTOINLENL = 0x00; SYNCDELAY;
    EP6AUTOINLENH = 0x02; SYNCDELAY;
    EP6AUTOINLENL = 0x00; SYNCDELAY;
    FIFOPINPOLAR = (flags & 0x20) ? 0x3F : 0x00; SYNCDELAY;
    if (flags & 0x40) {                      /* fixed flags: FLAGA=EP2 FF, FLAGB=EP2 PF, FLAGC=EP6 FF, FLAGD=EP6 PF */
        PINFLAGSAB = 0x98; SYNCDELAY;
        PINFLAGSCD = 0xE8; SYNCDELAY;
    } else {
        PINFLAGSAB = 0x00; SYNCDELAY;        /* indexed flags (default) */
        PINFLAGSCD = 0x00; SYNCDELAY;
    }
    fifo_reset_all();
    EP2FIFOCFG = 0x08 | ww; SYNCDELAY;
    EP6FIFOCFG = 0x0C | ww; SYNCDELAY;          /* AUTOIN | ZEROLENIN */
    EP4FIFOCFG = ww; SYNCDELAY;
    EP8FIFOCFG = ww; SYNCDELAY;
    EP4BCL = 0x80; SYNCDELAY;
    EP4BCL = 0x80; SYNCDELAY;

    if (flags & 0x02) ifc |= bmIFCLKSRC;    /* internal clock (else external IFCLK pin) */
    if (flags & 0x04) ifc |= bm3048MHZ;
    if (flags & 0x08) ifc |= bmIFCLKPOL;
    if ((flags & 0x10) && (flags & 0x02)) ifc |= bmIFCLKOE;
    if (flags & 0x80) ifc |= bmASYNC;

    if (mode == 0) {
        IFCONFIG = ifc | 0x03; SYNCDELAY;   /* slave FIFO: external master writes EP2/EP6 */
    } else {
        build_waveform(mode, (flags >> 8) & 0x07, (flags & 0x800) ? 1 : 0, (flags & 0x2000) ? 1 : 0);
        if (flags & 0x1000) GPIFREADYCFG |= 0x40; else GPIFREADYCFG &= ~0x40;   /* SAS: sync RDY sampling */
        SYNCDELAY;
        GPIFWFSELECT = 0xE4; SYNCDELAY;
        GPIFIDLECS = 0x00; SYNCDELAY;       /* don't drive data bus when idle */
        IFCONFIG = ifc | 0x02; SYNCDELAY;   /* GPIF master */
        gpif_set_tc32(0xFFFFFFFF);
        SYNCDELAY;
        while (!(GPIFTRIG & 0x80)) ;
        frame_mark = (mode == 4 || mode == 5);
        frame_count = 0;
        GPIFIRQ = bmGPIFWF; SYNCDELAY;
        GPIFTRIG = GPIFTRGRD | GPIF_EP6;    /* start FIFO read into EP6 */
    }
}

void loader(void);   /* resident 0xA0 loader, lives in segment LOADER (loader.c) */

/* ------------------------------------------------------------ main */
void main(void)
{
    got_sud = FALSE;
    frame_mark = 0;
    frame_count = 0;
    ctl_override = 0;
    uart_on = 0;
    rx_head = rx_tail = 0;

    SETCPUFREQ(CLK_48M);
    REVCTL = 0x03; SYNCDELAY;               /* enhanced packet handling (Cypress recommends) */
    /* FIFO/GPIF registers are clocked by IFCLK. The sensor side does not drive IFCLK,
     * so keep the internal 30 MHz clock running (not output on the pin) whenever we
     * configure FIFOs; ports mode, nothing driven. */
    IFCONFIG = 0x80; SYNCDELAY;                        /* like ICI's EEPROM stub: ports mode, ext IFCLK */
    OEA = 0; OEB = 0; OEC = 0; OED = 0; OEE = 0;   /* everything input */

    RENUMERATE_UNCOND();

    USBIRQ = 0xFF;                          /* drop anything pending from before a soft restart */
    EPIRQ = 0xFF;
    USE_USB_INTS();
    ENABLE_SUDAV();
    ENABLE_USBRESET();
    ENABLE_HISPEED();

    EP2CFG = 0xE2; SYNCDELAY;               /* IN  bulk 512 double (1 KB) */
    EP4CFG = 0xA0; SYNCDELAY;               /* OUT bulk 512 double (1 KB) */
    EP6CFG = 0xE0; SYNCDELAY;               /* IN  bulk 512 quad   (2 KB) - video */
    EP8CFG &= ~bmVALID; SYNCDELAY;          /* no room left in the 4 KB FIFO RAM */
    EP1INCFG &= ~bmVALID; SYNCDELAY;
    EP1OUTCFG &= ~bmVALID; SYNCDELAY;
    fifo_reset_all();
    EP4BCL = 0x80; SYNCDELAY;
    EP4BCL = 0x80; SYNCDELAY;

    EA = 1;
    handle_hispeed(HISPEED ? TRUE : FALSE);  /* we cleared the pending HISPEED irq above */

    while (TRUE) {
        if (got_sud) {
            handle_setupdata();
            got_sud = FALSE;
        }
        if (GPIFIRQ & bmGPIFWF) {               /* waveform hit a GINT state = frame boundary */
            GPIFIRQ = bmGPIFWF;
            if (frame_mark) {
                INPKTEND = 0x06; SYNCDELAY;     /* short (or zero-length) packet delimits the frame */
                ++frame_count;
            }
        }
    }
}

BOOL handle_get_descriptor(void) { return FALSE; }

BOOL handle_vendorcommand(BYTE cmd)
{
    WORD wValue = SETUP_VALUE();
    WORD wIndex = SETUP_INDEX();
    WORD wLength = SETUP_LENGTH();
    BYTE n, i;

    switch (cmd) {
    case VR_VERSION:
        EP0BUF[0] = 'X'; EP0BUF[1] = 'P'; EP0BUF[2] = 0; EP0BUF[3] = 1;
        ep0_send(4);
        return TRUE;

    case VR_READ_PINS:
        EP0BUF[0] = IOA; EP0BUF[1] = IOB; EP0BUF[2] = IOC; EP0BUF[3] = IOD;
        EP0BUF[4] = IOE; EP0BUF[5] = GPIFREADYSTAT; EP0BUF[6] = IFCONFIG;
        EP0BUF[7] = GPIFIDLECTL;
        ep0_send(8);
        return TRUE;

    case VR_ACTIVITY:
        sample_activity(wValue ? wValue : 1000);
        for (i = 0; i < 48; ++i) EP0BUF[i] = scratch[i];
        ep0_send(48);
        return TRUE;

    case VR_I2C_SCAN:
        for (i = 0; i < 16; ++i) scratch[i] = 0;
        for (n = 0x08; n < 0x78; ++n)
            if (i2c_probe(n)) scratch[n >> 3] |= (1 << (n & 7));
        for (i = 0; i < 16; ++i) EP0BUF[i] = scratch[i];
        ep0_send(16);
        return TRUE;

    case VR_I2C_READ:
        n = wLength > 64 ? 64 : (BYTE)wLength;
        if (!(wIndex & 0x100)) {
            scratch[0] = (BYTE)wIndex;
            if (!i2c_write((BYTE)wValue, 1, scratch, 0, scratch)) { STALLEP0(); return TRUE; }
        }
        if (!i2c_read((BYTE)wValue, n, scratch)) { STALLEP0(); return TRUE; }
        for (i = 0; i < n; ++i) EP0BUF[i] = scratch[i];
        ep0_send(n);
        return TRUE;

    case VR_I2C_WRITE:
        n = ep0_recv();
        if (!i2c_write((BYTE)wValue, n, scratch, 0, scratch)) STALLEP0();
        return TRUE;

    case VR_PORT_WRITE:
        switch (wValue & 7) {
        case 0: IOA = (BYTE)wIndex; break;
        case 1: IOB = (BYTE)wIndex; break;
        case 2: IOC = (BYTE)wIndex; break;
        case 3: IOD = (BYTE)wIndex; break;
        case 4: IOE = (BYTE)wIndex; break;
        }
        return TRUE;

    case VR_OE_WRITE:
        switch (wValue & 7) {
        case 0: OEA = (BYTE)wIndex; break;
        case 1: OEB = (BYTE)wIndex; break;
        case 2: OEC = (BYTE)wIndex; break;
        case 3: OED = (BYTE)wIndex; break;
        case 4: OEE = (BYTE)wIndex; break;
        }
        return TRUE;

    case VR_SET_CTL:
        ctl_levels = (BYTE)wValue;
        ctl_override = 1;
        GPIFIDLECTL = ctl_levels & 0x3F; SYNCDELAY;
        return TRUE;

    case VR_RDY_TRACE:
        rdy_trace((BYTE)wValue, wIndex ? wIndex : 20000);
        for (i = 0; i < 64; ++i) EP0BUF[i] = scratch[i];
        ep0_send(64);
        return TRUE;

    case VR_UART_SETUP:
        uart_setup(MAKEDWORD(wIndex, wValue));
        return TRUE;

    case VR_UART_TX:
        n = ep0_recv();
        if (!uart_on) uart_setup(57600);
        for (i = 0; i < n; ++i) uart_tx(scratch[i]);
        return TRUE;

    case VR_UART_RX:
        n = wLength > 64 ? 64 : (BYTE)wLength;
        for (i = 0; i < n && rx_tail != rx_head; ++i) {
            EP0BUF[i] = rxbuf[rx_tail];
            rx_tail = (rx_tail + 1) & (RXBUF_SIZE - 1);
        }
        ep0_send(i);
        return TRUE;

    case VR_XREG_READ: {
        __xdata BYTE *p = (__xdata BYTE *)wValue;
        n = wLength > 64 ? 64 : (BYTE)wLength;
        for (i = 0; i < n; ++i) EP0BUF[i] = p[i];
        ep0_send(n);
        return TRUE;
    }

    case VR_XREG_WRITE: {
        __xdata BYTE *p = (__xdata BYTE *)wValue;
        n = ep0_recv();
        for (i = 0; i < n; ++i) { p[i] = scratch[i]; SYNCDELAY; }
        return TRUE;
    }

    case VR_STREAM_START:
        stream_start((BYTE)wValue, wIndex);
        return TRUE;

    case VR_STREAM_STOP:
        stream_stop();
        return TRUE;

    case VR_COMMIT_TAIL:
        EP0BUF[0] = EP6FIFOBCL; EP0BUF[1] = EP6FIFOBCH;
        if (EP6FIFOBCH || EP6FIFOBCL) { INPKTEND = 0x06; SYNCDELAY; }
        ep0_send(2);
        return TRUE;

    case VR_STREAM_STATUS:
        EP0BUF[0] = IFCONFIG; EP0BUF[1] = GPIFTRIG; EP0BUF[2] = GPIFIDLECS;
        EP0BUF[3] = GPIFREADYSTAT; EP0BUF[4] = EP2CS; EP0BUF[5] = EP6CS;
        EP0BUF[6] = EP2468STAT; EP0BUF[7] = EP2FIFOFLGS; EP0BUF[8] = EP6FIFOFLGS;
        EP0BUF[9] = EP2FIFOBCH; EP0BUF[10] = EP2FIFOBCL; EP0BUF[11] = EP6FIFOBCH;
        EP0BUF[12] = EP6FIFOBCL; EP0BUF[13] = FIFOPINPOLAR; EP0BUF[14] = EP2FIFOCFG;
        EP0BUF[15] = IOA; EP0BUF[16] = LSB(frame_count); EP0BUF[17] = MSB(frame_count);
        ep0_send(18);
        return TRUE;

    case 0xA0:                                   /* Cypress loader protocol */
        if (!(SETUP_TYPE & 0x80) && wValue == 0xE600) {
            n = ep0_recv();
            if (n && (scratch[0] & 1)) loader();  /* never returns */
            return TRUE;
        }
        if (SETUP_TYPE & 0x80) {
            __xdata BYTE *p = (__xdata BYTE *)wValue;
            n = wLength > 64 ? 64 : (BYTE)wLength;
            for (i = 0; i < n; ++i) EP0BUF[i] = p[i];
            ep0_send(n);
            return TRUE;
        }
        return FALSE;
    }
    return FALSE;
}

BOOL handle_get_interface(BYTE ifc, BYTE *alt_ifc)
{
    if (ifc == 0) { *alt_ifc = 0; return TRUE; }
    return FALSE;
}

BOOL handle_set_interface(BYTE ifc, BYTE alt_ifc)
{
    if (ifc == 0 && alt_ifc == 0) {
        RESETTOGGLE(0x02);
        RESETTOGGLE(0x86);
        return TRUE;
    }
    return FALSE;
}

BYTE handle_get_configuration(void) { return 1; }
BOOL handle_set_configuration(BYTE cfg) { return cfg == 1 ? TRUE : FALSE; }

void sudav_isr(void) __interrupt (SUDAV_ISR)
{
    got_sud = TRUE;
    CLEAR_SUDAV();
}

void usbreset_isr(void) __interrupt (USBRESET_ISR)
{
    handle_hispeed(FALSE);
    CLEAR_USBRESET();
}

void hispeed_isr(void) __interrupt (HISPEED_ISR)
{
    handle_hispeed(TRUE);
    CLEAR_HISPEED();
}
