#!/usr/bin/env python3
"""
explorer.py - host side of the ICI 9320P explorer firmware.

Loads firmware/explorer/build/explorer.ihx into the bare camera (2aa9:9f00) if
needed, then talks to it (2aa9:9f10) over EP0 vendor requests.

  explorer.py pins                      # port pin levels + RDY pins
  explorer.py activity [-n ITERS] [-r REPEAT]   # which pins toggle
  explorer.py i2cscan
  explorer.py i2cread ADDR REG N | i2cwrite ADDR BYTES...
  explorer.py uart --baud 57600 --tx 6e0000000000... --wait 0.2
  explorer.py tau [--baud 57600]       # FLIR Tau serial protocol probe
  explorer.py stream --mode gpif-rdy --rdy 0 --width 16 --bytes 2000000 -o captures/x.bin
  explorer.py analyze FILE              # look for frame/line structure in a dump
  explorer.py xread ADDR N | xwrite ADDR BYTES...
  explorer.py stop
"""
import argparse
import os
import struct
import sys
import time
from collections import Counter

import usb.core
import usb.util

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from ici9320 import fx2loader  # noqa: E402

VID = 0x2AA9
PID_BARE = 0x9F00
PID_EXPLORER = 0x9F10
FW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "firmware", "explorer", "build", "explorer.ihx")

VR = dict(READ_PINS=0x10, ACTIVITY=0x11, I2C_SCAN=0x12, I2C_READ=0x13, I2C_WRITE=0x14,
          PORT_WRITE=0x15, OE_WRITE=0x16, UART_SETUP=0x20, UART_TX=0x21, UART_RX=0x22,
          XREG_READ=0x30, XREG_WRITE=0x31, STREAM_START=0x40, STREAM_STOP=0x41,
          STREAM_STATUS=0x42, COMMIT_TAIL=0x43, VERSION=0x50, RDY_TRACE=0x17)


class Explorer:
    def __init__(self, timeout=3000):
        self.timeout = timeout
        self.dev = self._connect()

    def _connect(self):
        dev = usb.core.find(idVendor=VID, idProduct=PID_EXPLORER)
        if dev is None:
            bare = usb.core.find(idVendor=VID, idProduct=PID_BARE)
            if bare is None:
                sys.exit("no camera found (neither 2aa9:9f00 nor 2aa9:9f10)")
            if not os.path.exists(FW):
                sys.exit(f"firmware missing: build it with 'make -C firmware/explorer' ({FW})")
            print("loading explorer firmware into bare FX2 ...", file=sys.stderr)
            with open(FW) as f:
                fx2loader.load_firmware(bare, fx2loader.parse_ihex(f.read()))
            usb.util.dispose_resources(bare)
            dev = fx2loader.wait_for_device(VID, PID_EXPLORER, timeout=8)
            if dev is None:
                sys.exit("explorer firmware did not re-enumerate as 2aa9:9f10")
            time.sleep(0.3)
        dev.set_configuration()
        usb.util.claim_interface(dev, 0)
        v = self.ctrl_in(VR["VERSION"], 0, 0, 4, dev=dev)
        if v[:2] != b"XP":
            sys.exit(f"unexpected VERSION reply {v.hex()}")
        return dev

    def ctrl_in(self, req, value=0, index=0, length=64, dev=None, timeout=None):
        return bytes((dev or self.dev).ctrl_transfer(0xC0, req, value, index, length, timeout or self.timeout))

    def ctrl_out(self, req, value=0, index=0, data=b"", timeout=None):
        return self.dev.ctrl_transfer(0x40, req, value, index, data, timeout or self.timeout)

    # ---------------------------------------------------------------- pins
    def pins(self):
        d = self.ctrl_in(VR["READ_PINS"], 0, 0, 8)
        return dict(A=d[0], B=d[1], C=d[2], D=d[3], E=d[4], RDY=d[5] & 0x3F, IFCONFIG=d[6], GPIFIDLECTL=d[7])

    def rdy_trace(self, mask=1, iters=20000):
        d = self.ctrl_in(VR["RDY_TRACE"], mask, iters, 64, timeout=20000)
        n = d[63]
        return [(d[i * 3], d[i * 3 + 1] | (d[i * 3 + 2] << 8)) for i in range(n)]

    def activity(self, iters=2000):
        d = self.ctrl_in(VR["ACTIVITY"], iters, 0, 48, timeout=20000)
        return dict(A=d[0:8], B=d[8:16], C=d[16:24], D=d[24:32], E=d[32:40], RDY=d[40:46])

    # ---------------------------------------------------------------- i2c
    def i2c_scan(self):
        bm = self.ctrl_in(VR["I2C_SCAN"], 0, 0, 16, timeout=20000)
        return [a for a in range(128) if bm[a >> 3] & (1 << (a & 7))]

    def i2c_read(self, addr, reg, n):
        idx = 0x100 if reg is None else reg
        return self.ctrl_in(VR["I2C_READ"], addr, idx, n)

    def i2c_write(self, addr, data):
        return self.ctrl_out(VR["I2C_WRITE"], addr, 0, bytes(data))

    # ---------------------------------------------------------------- uart
    def uart_setup(self, baud):
        self.ctrl_out(VR["UART_SETUP"], baud & 0xFFFF, baud >> 16)

    def uart_tx(self, data):
        for i in range(0, len(data), 64):
            self.ctrl_out(VR["UART_TX"], 0, 0, bytes(data[i:i + 64]))

    def uart_rx(self, wait=0.2):
        time.sleep(wait)
        out = b""
        while True:
            d = self.ctrl_in(VR["UART_RX"], 0, 0, 64)
            out += d
            if len(d) < 64:
                break
        return out

    # ---------------------------------------------------------------- xdata
    def xread(self, addr, n):
        return self.ctrl_in(VR["XREG_READ"], addr, 0, n)

    def xwrite(self, addr, data):
        return self.ctrl_out(VR["XREG_WRITE"], addr, 0, bytes(data))

    # ---------------------------------------------------------------- stream
    MODES = {"slave": 0, "gpif-rdy": 1, "gpif-free": 2, "gpif-edge": 3, "frame": 4, "frames": 5}

    def stream_start(self, mode, width16=True, clock="ext", pol=False, drive_clk=False, hi_pins=False,
                     rdy=0, fixed_flags=False, async_=False, gate_full=False, sas=False, inv=False):
        flags = (1 if width16 else 0)
        if clock != "ext":
            flags |= 0x02
            if clock == "int48":
                flags |= 0x04
            if drive_clk:
                flags |= 0x10
        if pol:
            flags |= 0x08
        if hi_pins:
            flags |= 0x20
        if fixed_flags:
            flags |= 0x40
        if async_:
            flags |= 0x80
        flags |= (rdy & 7) << 8
        if gate_full:
            flags |= 0x800
        if sas:
            flags |= 0x1000
        if inv:
            flags |= 0x2000
        self.ctrl_out(VR["STREAM_START"], self.MODES[mode], flags)

    def stream_stop(self):
        self.ctrl_out(VR["STREAM_STOP"])

    def reload_firmware(self, path=FW):
        """Upload a new image through the firmware's resident 0xA0 loader and restart."""
        with open(path) as f:
            image = fx2loader.parse_ihex(f.read())
        fx2loader.load_firmware(self.dev, image)      # CPUCS=1 -> loader, chunks, CPUCS=0 -> restart
        usb.util.dispose_resources(self.dev)
        time.sleep(0.5)
        self.dev = fx2loader.wait_for_device(VID, PID_EXPLORER, timeout=8)
        if self.dev is None:
            sys.exit("device did not come back after reload")
        time.sleep(0.3)
        self.dev.set_configuration()
        usb.util.claim_interface(self.dev, 0)

    def stream_status(self):
        d = self.ctrl_in(VR["STREAM_STATUS"], 0, 0, 18)
        names = ["IFCONFIG", "GPIFTRIG", "GPIFIDLECS", "GPIFREADYSTAT", "EP2CS", "EP6CS", "EP2468STAT",
                 "EP2FIFOFLGS", "EP6FIFOFLGS", "EP2FIFOBCH", "EP2FIFOBCL", "EP6FIFOBCH", "EP6FIFOBCL",
                 "FIFOPINPOLAR", "EP2FIFOCFG", "IOA"]
        st = dict(zip(names, d))
        st["FRAMES"] = d[16] | (d[17] << 8) if len(d) >= 18 else -1
        return st

    def bulk_read(self, nbytes, ep=0x86, chunk=65536, timeout=1000):
        out = bytearray()
        t0 = time.time()
        timeouts = 0
        while len(out) < nbytes:
            try:
                d = self.dev.read(ep, min(chunk, nbytes - len(out)), timeout)
                out += bytes(d)
            except usb.core.USBTimeoutError:
                timeouts += 1
                if timeouts >= 3:
                    break
        return bytes(out), time.time() - t0, timeouts


# -------------------------------------------------------------------- helpers

def fmt_bits(v):
    return f"{v:08b}"


def tau_packet(func, data=b""):
    """FLIR Tau/Tau2 serial protocol frame."""
    def crc16(buf):
        crc = 0
        for b in buf:
            crc ^= b << 8
            for _ in range(8):
                crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
                crc &= 0xFFFF
        return crc
    hdr = bytes([0x6E, 0x00, 0x00, func]) + struct.pack(">H", len(data))
    hdr += struct.pack(">H", crc16(hdr))
    pkt = hdr + data
    return pkt + struct.pack(">H", crc16(pkt))


def save_png(data, path, width=320):
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        print("(numpy/PIL missing, no png)")
        return
    w = np.frombuffer(data[: (len(data) // (2 * width)) * 2 * width], dtype="<u2").reshape(-1, width)
    if w.size == 0:
        return
    lo, hi = np.percentile(w, 0.5), np.percentile(w, 99.5)
    img = np.clip((w.astype(np.float32) - lo) / max(hi - lo, 1) * 255, 0, 255).astype(np.uint8)
    Image.fromarray(img).save(path)
    print(f"saved {path} ({w.shape[1]}x{w.shape[0]}, raw range {w.min()}-{w.max()}, stretch {lo:.0f}-{hi:.0f})")


def analyze(buf, width16=True):
    print(f"{len(buf)} bytes")
    if not buf:
        return
    bc = Counter(buf)
    print(f"distinct byte values: {len(bc)}; most common: " +
          ", ".join(f"{b:02x}x{n}" for b, n in bc.most_common(8)))
    if width16 and len(buf) >= 4:
        n = len(buf) // 2
        w = struct.unpack(f"<{n}H", buf[:n * 2])
        wc = Counter(w)
        print(f"16-bit LE: min {min(w):04x} max {max(w):04x} distinct {len(wc)}; "
              f"most common: " + ", ".join(f"{v:04x}x{c}" for v, c in wc.most_common(6)))
        hi = Counter(v >> 14 for v in w)
        print(f"top-2-bits histogram (14-bit data would keep them 0): {dict(hi)}")
        # candidate sync words: values that appear ~ once per line or frame
        for v, c in wc.most_common(40):
            pos = [i for i, x in enumerate(w[:200000]) if x == v]
            if len(pos) > 3:
                gaps = Counter(b - a for a, b in zip(pos, pos[1:]))
                g, gc = gaps.most_common(1)[0]
                if gc >= len(pos) * 0.5 and g > 8:
                    print(f"  value {v:04x} repeats every {g} words ({gc}/{len(pos)-1} gaps) -> candidate sync/line marker")
    # autocorrelation on bytes to find line period (fast, approximate)
    try:
        import numpy as np
        x = np.frombuffer(buf[: min(len(buf), 1 << 20)], dtype=np.uint8).astype(np.float32)
        x -= x.mean()
        f = np.fft.rfft(x)
        ac = np.fft.irfft(f * np.conj(f))[: 4096]
        ac /= ac[0] if ac[0] else 1
        peaks = sorted(((ac[k], k) for k in range(16, len(ac))), reverse=True)[:8]
        print("autocorrelation peaks (lag bytes, strength): " +
              ", ".join(f"{k}:{v:.2f}" for v, k in peaks))
    except ImportError:
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pins")
    p = sub.add_parser("activity"); p.add_argument("-n", type=int, default=2000); p.add_argument("-r", type=int, default=3)
    sub.add_parser("i2cscan")
    p = sub.add_parser("i2cread"); p.add_argument("addr", type=lambda s: int(s, 0)); p.add_argument("reg", type=lambda s: None if s == "-" else int(s, 0)); p.add_argument("n", type=int)
    p = sub.add_parser("i2cwrite"); p.add_argument("addr", type=lambda s: int(s, 0)); p.add_argument("bytes", nargs="+", type=lambda s: int(s, 0))
    p = sub.add_parser("uart"); p.add_argument("--baud", type=int, default=57600); p.add_argument("--tx", default=""); p.add_argument("--wait", type=float, default=0.3)
    p = sub.add_parser("tau"); p.add_argument("--baud", type=int, nargs="*", default=[57600, 115200, 9600, 921600])
    p = sub.add_parser("stream"); p.add_argument("--mode", choices=list(Explorer.MODES), default="gpif-rdy")
    p.add_argument("--rdy", type=int, default=0); p.add_argument("--width", type=int, choices=[8, 16], default=16)
    p.add_argument("--clock", choices=["ext", "int30", "int48"], default="ext"); p.add_argument("--pol", action="store_true")
    p.add_argument("--drive-clk", action="store_true"); p.add_argument("--hi-pins", action="store_true")
    p.add_argument("--bytes", type=int, default=1 << 20); p.add_argument("-o", "--out")
    p.add_argument("--ep", type=lambda s: int(s, 0), default=None, help="0x82 or 0x86 (default: 0x82 for slave, 0x86 for gpif)")
    p.add_argument("--fixed-flags", action="store_true"); p.add_argument("--async", dest="async_", action="store_true")
    p.add_argument("--timeout", type=int, default=1000)
    p = sub.add_parser("analyze"); p.add_argument("file"); p.add_argument("--width", type=int, default=16)
    p = sub.add_parser("xread"); p.add_argument("addr", type=lambda s: int(s, 0)); p.add_argument("n", type=int, default=16, nargs="?")
    p = sub.add_parser("xwrite"); p.add_argument("addr", type=lambda s: int(s, 0)); p.add_argument("bytes", nargs="+", type=lambda s: int(s, 0))
    p = sub.add_parser("portwrite"); p.add_argument("port", choices="ABCDE"); p.add_argument("value", type=lambda s: int(s, 0)); p.add_argument("--oe", type=lambda s: int(s, 0), default=None)
    p = sub.add_parser("rdytrace", help="run-length trace of an RDY pin (default RDY0)")
    p.add_argument("--mask", type=lambda s: int(s, 0), default=1); p.add_argument("-n", type=int, default=20000)
    p = sub.add_parser("frame", help="capture one RDY0-aligned frame, save .bin and .png")
    p.add_argument("-o", "--out", default="captures/frame"); p.add_argument("--width", type=int, default=320)
    p.add_argument("--sas", action="store_true"); p.add_argument("--inv", action="store_true")
    p = sub.add_parser("grab", help="capture N continuous frames (mode 5), report timing")
    p.add_argument("-n", type=int, default=60); p.add_argument("--frame-bytes", type=int, default=153600)
    p.add_argument("-o", "--out", default=None); p.add_argument("--sas", action="store_true"); p.add_argument("--inv", action="store_true")
    sub.add_parser("stop")
    sub.add_parser("status")
    sub.add_parser("reload", help="reload firmware/explorer/build/explorer.ihx through the resident loader")
    p = sub.add_parser("sweep", help="try slave-FIFO configurations and report which yields data")
    p.add_argument("--bytes", type=int, default=262144); p.add_argument("--timeout", type=int, default=800)
    args = ap.parse_args()

    if args.cmd == "analyze":
        with open(args.file, "rb") as f:
            analyze(f.read(), args.width == 16)
        return

    x = Explorer()

    if args.cmd == "pins":
        p = x.pins()
        for k in "ABCDE":
            print(f"port {k}: {fmt_bits(p[k])}  (0x{p[k]:02x})   bit7..bit0")
        print(f"RDY5..0: {p['RDY']:06b}   IFCONFIG=0x{p['IFCONFIG']:02x} GPIFIDLECTL=0x{p['GPIFIDLECTL']:02x}")

    elif args.cmd == "activity":
        print(f"transition counts per pin over {args.n} samples (255 = saturated), {args.r} runs")
        for run in range(args.r):
            a = x.activity(args.n)
            for k in "ABCDE":
                print(f"  port {k}: " + " ".join(f"{k}{i}={a[k][i]:3d}" for i in range(8)))
            print("  RDY:    " + " ".join(f"RDY{i}={a['RDY'][i]:3d}" for i in range(6)))
            if run < args.r - 1:
                print("  --")

    elif args.cmd == "i2cscan":
        found = x.i2c_scan()
        print("I2C devices (7-bit): " + (", ".join(f"0x{a:02x}" for a in found) or "none"))
        print("(0x50-0x57 = the FX2 boot EEPROM; anything else is sensor-side)")

    elif args.cmd == "i2cread":
        print(x.i2c_read(args.addr, args.reg, args.n).hex(" "))

    elif args.cmd == "i2cwrite":
        x.i2c_write(args.addr, args.bytes); print("ok")

    elif args.cmd == "uart":
        x.uart_setup(args.baud)
        if args.tx:
            x.uart_tx(bytes.fromhex(args.tx))
        rx = x.uart_rx(args.wait)
        print(f"rx {len(rx)} bytes: {rx.hex(' ')}")

    elif args.cmd == "tau":
        probes = [("NO_OP", tau_packet(0x00)), ("GET_REVISION", tau_packet(0x05)),
                  ("SERIAL_NUMBER", tau_packet(0x04)), ("CAMERA_PART", tau_packet(0x66))]
        for baud in args.baud:
            x.uart_setup(baud)
            x.uart_rx(0.1)  # flush
            for name, pkt in probes:
                x.uart_tx(pkt)
                rx = x.uart_rx(0.25)
                print(f"{baud:7d} {name:14s} tx {pkt.hex()} -> rx {len(rx)} bytes {rx.hex(' ')}")

    elif args.cmd == "stream":
        ep = args.ep if args.ep is not None else (0x82 if args.mode == "slave" else 0x86)
        x.stream_start(args.mode, args.width == 16, args.clock, args.pol, args.drive_clk, args.hi_pins,
                       args.rdy, args.fixed_flags, args.async_)
        time.sleep(0.05)
        print("status after start:", {k: f"{v:02x}" for k, v in x.stream_status().items()})
        data, dt, to = x.bulk_read(args.bytes, ep, timeout=args.timeout)
        st = x.stream_status()
        x.stream_stop()
        print(f"got {len(data)} bytes in {dt:.2f}s ({len(data)/dt/1e6 if dt else 0:.2f} MB/s), {to} timeouts")
        print("status before stop:", {k: f"{v:02x}" for k, v in st.items()})
        if args.out and data:
            with open(args.out, "wb") as f:
                f.write(data)
            print("saved", args.out)
        analyze(data, args.width == 16)

    elif args.cmd == "xread":
        print(x.xread(args.addr, args.n).hex(" "))

    elif args.cmd == "xwrite":
        x.xwrite(args.addr, args.bytes); print("ok")

    elif args.cmd == "portwrite":
        idx = "ABCDE".index(args.port)
        if args.oe is not None:
            x.ctrl_out(VR["OE_WRITE"], idx, args.oe)
        x.ctrl_out(VR["PORT_WRITE"], idx, args.value); print("ok")

    elif args.cmd == "stop":
        x.stream_stop(); print("stopped")

    elif args.cmd == "rdytrace":
        t0 = time.time(); runs = x.rdy_trace(args.mask, args.n); dt = time.time() - t0
        total = sum(r for _, r in runs) or 1
        print(f"{len(runs)} runs over {args.n} samples (~{dt*1e6/args.n:.2f} us/sample incl. USB):")
        for lvl, r in runs:
            print(f"  level {lvl:02x} for {r:6d} samples (~{r*dt/args.n*1e3:.3f} ms)")

    elif args.cmd == "frame":
        x.stream_start("frame", width16=True, clock="int48", sas=args.sas, inv=args.inv)
        data, dt, to = x.bulk_read(1 << 20, 0x86, chunk=153600, timeout=400)
        tail = x.ctrl_in(VR["COMMIT_TAIL"], 0, 0, 2)
        ntail = tail[0] | (tail[1] << 8)
        if ntail:
            d2, _, _ = x.bulk_read(ntail, 0x86, chunk=1024, timeout=300)
            data += d2
        st = x.stream_status()
        x.stream_stop()
        print(f"tail committed: {ntail} bytes")
        print(f"frame: {len(data)} bytes ({len(data)/2} words = {len(data)/2/args.width:.2f} rows of {args.width}); "
              f"GPIFTRIG={st['GPIFTRIG']:02x} EP6FLGS={st['EP6FIFOFLGS']:02x}")
        with open(args.out + ".bin", "wb") as f:
            f.write(data)
        save_png(data, args.out + ".png", args.width)

    elif args.cmd == "grab":
        x.stream_start("frames", width16=True, clock="int48", sas=args.sas, inv=args.inv)
        frames, sizes, timeouts = [], [], 0
        t0 = time.time()
        while len(frames) < args.n:
            try:
                d = bytes(x.dev.read(0x86, args.frame_bytes, 1000))
            except usb.core.USBTimeoutError:
                timeouts += 1
                if timeouts >= 3:
                    break
                continue
            if len(d) == 0:
                continue  # zero-length delimiter after a complete frame
            frames.append(d); sizes.append(len(d))
        dt = time.time() - t0
        st = x.stream_status()
        x.stream_stop()
        from collections import Counter
        good = sum(1 for n in sizes if n == args.frame_bytes)
        print(f"{len(frames)} frames in {dt:.3f}s -> {len(frames)/dt:.1f} fps; complete {good}, "
              f"sizes {dict(Counter(sizes).most_common(5))}; firmware frame_count={st['FRAMES']}; timeouts={timeouts}")
        if args.out and frames:
            with open(args.out, "wb") as f:
                for d in frames:
                    f.write(d)
            save_png(frames[0], args.out + ".png", 320)
            save_png(frames[-1], args.out + "_last.png", 320)

    elif args.cmd == "reload":
        x.reload_firmware()
        print("reloaded; device present:", usb.core.find(idVendor=VID, idProduct=PID_EXPLORER) is not None)

    elif args.cmd == "sweep":
        best = None
        for w16 in (True, False):
            for hi in (False, True):
                for asy in (False, True):
                    for fixed in (False, True):
                        x.stream_start("slave", width16=w16, hi_pins=hi, async_=asy, fixed_flags=fixed)
                        time.sleep(0.1)
                        data, dt, to = x.bulk_read(args.bytes, 0x82, timeout=args.timeout)
                        st = x.stream_status()
                        x.stream_stop()
                        tag = f"w16={int(w16)} hi={int(hi)} async={int(asy)} fixed={int(fixed)}"
                        print(f"{tag}: {len(data):7d} bytes in {dt:.2f}s; EP2CS={st['EP2CS']:02x} "
                              f"EP2468STAT={st['EP2468STAT']:02x} EP2FLGS={st['EP2FIFOFLGS']:02x} "
                              f"EP2BC={st['EP2FIFOBCH']:02x}{st['EP2FIFOBCL']:02x} RDY={st['GPIFREADYSTAT']:02x} IOA={st['IOA']:02x}")
                        if data:
                            print("    first 48:", data[:48].hex(" "))
                            if best is None or len(data) > best[0]:
                                best = (len(data), tag)
        print("best:", best)


if __name__ == "__main__":
    main()
