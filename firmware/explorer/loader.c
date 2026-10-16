/* Resident Cypress-0xA0 loader for the explorer firmware. Compiled with
 * --codeseg LOADER and linked at 0x3A00 (see Makefile) so that a new image can
 * overwrite everything else while this code runs. Must not call anything. */
#include <fx2regs.h>
#include <fx2macros.h>

/* ------------------------------------------------------------ resident loader
 * Lives in its own code segment (LOADER, 0x3A00-0x3BFF, see Makefile) and calls
 * nothing outside it, so the host can overwrite the rest of RAM with a new
 * image while it runs. Protocol = Cypress 0xA0: CPUCS(0xE600)=1 enters the
 * loader, 0xA0 writes/reads RAM, CPUCS=0 restarts the firmware from address 0.
 * Writes into the loader's own segment are ignored (keep the loader stable).  */
#define LOADER_LO 0x3A00
#define LOADER_HI 0x3C00
void loader(void)
{
    BYTE bmrt, n, i, v;
    WORD addr, remaining;
    __xdata BYTE *p;
    EA = 0;
    EP0CS |= bmHSNAK;                    /* finish the CPUCS=1 request */
    for (;;) {
        while (!(USBIRQ & bmSUDAV)) ;
        USBIRQ = bmSUDAV;
        bmrt = SETUPDAT[0];
        addr = MAKEWORD(SETUPDAT[3], SETUPDAT[2]);
        remaining = MAKEWORD(SETUPDAT[7], SETUPDAT[6]);
        if (SETUPDAT[1] != 0xA0) {
            EP0CS |= bmEPSTALL;
            continue;
        }
        p = (__xdata BYTE *)addr;
        if (bmrt & 0x80) {                                   /* IN: read RAM */
            while (remaining) {
                n = remaining > 64 ? 64 : (BYTE)remaining;
                for (i = 0; i < n; ++i) EP0BUF[i] = p[i];
                p += n;
                remaining -= n;
                EP0BCH = 0;
                EP0BCL = n;
                while (EP0CS & bmEPBUSY) ;
            }
            EP0CS |= bmHSNAK;
        } else {                                             /* OUT: write RAM */
            v = 1;
            while (remaining) {
                EP0BCH = 0;
                EP0BCL = 0;                                  /* arm */
                while (EP0CS & bmEPBUSY) ;
                n = EP0BCL;
                if (!n) break;
                v = EP0BUF[0];
                if (addr != 0xE600 && !(addr >= LOADER_LO && addr < LOADER_HI))
                    for (i = 0; i < n; ++i) p[i] = EP0BUF[i];
                p += n;
                remaining -= n;
            }
            EP0CS |= bmHSNAK;
            if (addr == 0xE600 && !(v & 1)) {
                __asm
                    ljmp 0x0000
                __endasm;
            }
        }
    }
}
