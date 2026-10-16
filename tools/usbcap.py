#!/usr/bin/env python3
"""
usbcap.py - analyse a USB capture of the ICI 9320P talking to its Windows driver.

Reads .pcap / .pcapng files produced by
  * USBPcap on Windows            (linktype 249)
  * Linux usbmon via Wireshark    (linktype 220 "mmapped", or 189)
  * tools/usbmon_capture.py       (linktype 220)

and prints / extracts:
  * every control (vendor) request, with setup packet and payload
  * the Cypress FX2 firmware image uploaded with vendor request 0xA0
    (written to --fw-out as Intel HEX + raw 64 KiB bin)
  * per-endpoint bulk/interrupt traffic statistics and payload samples
  * a timeline so the vendor protocol (init sequence, frame framing) can be
    reconstructed

Usage:
  usbcap.py capture.pcapng                      # summary
  usbcap.py capture.pcapng --fw-out firmware/   # also extract FX2 firmware
  usbcap.py capture.pcapng --ctrl               # dump every control request
  usbcap.py capture.pcapng --bulk 0x86 --dump 2 # hex-dump first 2 transfers of EP 0x86
  usbcap.py capture.pcapng --raw out_dir        # save every bulk IN payload as files

Pure python, no dependencies (works on the Jetson too).
"""
import argparse
import os
import struct
import sys
from collections import defaultdict, OrderedDict

# ---------------------------------------------------------------- pcap / pcapng

LINKTYPE_USB_LINUX = 189
LINKTYPE_USB_LINUX_MMAPPED = 220
LINKTYPE_USBPCAP = 249


def read_pcap_packets(path):
    """Yield (linktype, timestamp_seconds, bytes) for each packet."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] == b"\x0a\x0d\x0d\x0a":
        yield from _read_pcapng(data)
    else:
        yield from _read_pcap(data)


def _read_pcap(data):
    magic = data[:4]
    if magic == b"\xd4\xc3\xb2\xa1":
        end, nano = "<", False
    elif magic == b"\xa1\xb2\xc3\xd4":
        end, nano = ">", False
    elif magic == b"\x4d\x3c\xb2\xa1":
        end, nano = "<", True
    elif magic == b"\xa1\xb2\x3c\x4d":
        end, nano = ">", True
    else:
        raise ValueError("not a pcap/pcapng file")
    _, _, _, _, _, linktype = struct.unpack(end + "HHiIII", data[4:24])
    off = 24
    while off + 16 <= len(data):
        ts_sec, ts_frac, incl, orig = struct.unpack(end + "IIII", data[off:off + 16])
        off += 16
        pkt = data[off:off + incl]
        off += incl
        ts = ts_sec + ts_frac / (1e9 if nano else 1e6)
        yield linktype, ts, pkt


def _read_pcapng(data):
    off = 0
    end = "<"
    ifaces = []
    while off + 12 <= len(data):
        btype, = struct.unpack(end + "I", data[off:off + 4])
        if btype == 0x0A0D0D0A:  # section header: detect endianness
            bom = data[off + 8:off + 12]
            end = "<" if bom == b"\x4d\x3c\x2b\x1a" else ">"
            ifaces = []
        blen, = struct.unpack(end + "I", data[off + 4:off + 8])
        if blen < 12:
            break
        body = data[off + 8:off + blen - 4]
        if btype == 1:  # interface description
            linktype, _, snaplen = struct.unpack(end + "HHI", body[:8])
            tsres = 6
            o = 8
            while o + 4 <= len(body):
                code, olen = struct.unpack(end + "HH", body[o:o + 4])
                val = body[o + 4:o + 4 + olen]
                if code == 9 and olen >= 1:  # if_tsresol
                    v = val[0]
                    tsres = v & 0x7F if not (v & 0x80) else None
                    if v & 0x80:
                        tsres = ("pow2", v & 0x7F)
                if code == 0:
                    break
                o += 4 + ((olen + 3) & ~3)
            ifaces.append((linktype, tsres))
        elif btype == 6:  # enhanced packet block
            ifid, tsh, tsl, caplen, origlen = struct.unpack(end + "IIIII", body[:20])
            pkt = body[20:20 + caplen]
            linktype, tsres = ifaces[ifid] if ifid < len(ifaces) else (0, 6)
            t = (tsh << 32) | tsl
            if isinstance(tsres, tuple):
                ts = t / float(2 ** tsres[1])
            else:
                ts = t / float(10 ** tsres)
            yield linktype, ts, pkt
        elif btype == 3:  # simple packet block
            origlen, = struct.unpack(end + "I", body[:4])
            linktype, _ = ifaces[0] if ifaces else (0, 6)
            yield linktype, 0.0, body[4:4 + origlen]
        off += blen


# ---------------------------------------------------------------- USB records


class Rec:
    """Normalised USB event."""
    __slots__ = ("ts", "bus", "dev", "ep", "xfer", "is_in", "submit", "setup",
                 "data", "status", "length", "seq", "ctrl_stage")

    XFER_NAMES = {0: "iso", 1: "intr", 2: "ctrl", 3: "bulk"}

    def __init__(self):
        self.setup = None
        self.data = b""
        self.status = 0
        self.length = 0
        self.ctrl_stage = None

    @property
    def xfer_name(self):
        return self.XFER_NAMES.get(self.xfer, "?")

    def __repr__(self):
        return (f"<{self.ts:.6f} {'S' if self.submit else 'C'} {self.xfer_name} "
                f"dev{self.dev} ep{self.ep:02x} len={self.length} data={len(self.data)}>")


def parse_linux_usbmon(pkt, ts, mmapped):
    hdr = 64 if mmapped else 48
    if len(pkt) < hdr:
        return None
    (urb_id, etype, xfer, epnum, devnum, busnum, flag_setup, flag_data,
     ts_sec, ts_usec, status, length, len_cap) = struct.unpack("<QBBBBHbbqiiII", pkt[:40])
    setup = pkt[40:48]
    r = Rec()
    r.ts = ts_sec + ts_usec / 1e6 if ts_sec else ts
    r.bus, r.dev, r.ep = busnum, devnum, epnum
    r.xfer = xfer
    r.is_in = bool(epnum & 0x80)
    r.submit = (etype == ord("S"))
    r.status = status
    r.length = length
    r.setup = setup if flag_setup == 0 else None
    r.data = pkt[hdr:hdr + len_cap]
    return r


def parse_usbpcap(pkt, ts):
    if len(pkt) < 27:
        return None
    (hlen, irp_id, status, func, info, bus, dev, ep, xfer, dlen) = struct.unpack(
        "<HQiHBHHBBI", pkt[:27])
    r = Rec()
    r.ts = ts
    r.bus, r.dev, r.ep = bus, dev, ep
    r.xfer = {0: 0, 1: 1, 2: 2, 3: 3}.get(xfer, xfer)  # same numbering as usbmon
    r.is_in = bool(ep & 0x80)
    r.submit = not (info & 1)  # 0 = host->device (request), 1 = completion
    r.status = status
    r.length = dlen
    payload = pkt[hlen:hlen + dlen]
    if xfer == 2:
        stage = pkt[27] if hlen >= 28 else 0
        if stage == 0 and len(payload) >= 8:  # setup stage
            r.setup = payload[:8]
            r.data = payload[8:]
        else:
            r.data = payload
        r.ctrl_stage = stage
    else:
        r.data = payload
    return r


def load_records(path):
    recs = []
    seq = 0
    for linktype, ts, pkt in read_pcap_packets(path):
        if linktype == LINKTYPE_USBPCAP:
            r = parse_usbpcap(pkt, ts)
        elif linktype in (LINKTYPE_USB_LINUX, LINKTYPE_USB_LINUX_MMAPPED):
            r = parse_linux_usbmon(pkt, ts, linktype == LINKTYPE_USB_LINUX_MMAPPED)
        else:
            raise SystemExit(f"unsupported linktype {linktype}")
        if r is None:
            continue
        r.seq = seq
        seq += 1
        recs.append(r)
    return recs


# ---------------------------------------------------------------- control xfers


class Ctrl:
    __slots__ = ("ts", "dev", "bmRequestType", "bRequest", "wValue", "wIndex",
                 "wLength", "data", "status", "seq")

    @property
    def is_in(self):
        return bool(self.bmRequestType & 0x80)

    @property
    def rtype(self):
        return {0: "std", 1: "class", 2: "vendor", 3: "rsvd"}[(self.bmRequestType >> 5) & 3]

    def describe(self):
        d = ""
        if self.rtype == "std":
            names = {0: "GET_STATUS", 1: "CLEAR_FEATURE", 3: "SET_FEATURE", 5: "SET_ADDRESS",
                     6: "GET_DESCRIPTOR", 7: "SET_DESCRIPTOR", 8: "GET_CONFIGURATION",
                     9: "SET_CONFIGURATION", 10: "GET_INTERFACE", 11: "SET_INTERFACE"}
            d = names.get(self.bRequest, "")
        elif self.rtype == "vendor":
            d = {0xA0: "FX2_RAM_ACCESS", 0xA2: "FX2_EEPROM(Vend_Ax)", 0xA3: "FX2_XRAM",
                 0xA9: "FX2_EEPROM_LARGE(Vend_Ax)"}.get(self.bRequest, "")
            if self.bRequest == 0xA0 and self.wValue == 0xE600 and self.data:
                d += " CPUCS=" + ("RESET" if self.data[0] & 1 else "RUN")
        return d


def pair_controls(recs):
    """Match submit/complete pairs of control transfers into Ctrl objects."""
    ctrls = []
    pending = {}  # (dev) -> Ctrl awaiting completion (usbmon pairs by urb id but seq is enough)
    for r in recs:
        if r.xfer != 2:
            continue
        if r.submit and r.setup:
            c = Ctrl()
            c.ts, c.dev, c.seq = r.ts, r.dev, r.seq
            (c.bmRequestType, c.bRequest, c.wValue, c.wIndex, c.wLength) = struct.unpack(
                "<BBHHH", r.setup)
            c.data = bytes(r.data) if not c.is_in else b""
            c.status = None
            if c.is_in:
                pending[r.dev] = c
            ctrls.append(c)
        elif not r.submit:
            c = pending.pop(r.dev, None)
            if c is not None:
                c.data = bytes(r.data)
                c.status = r.status
            elif r.setup:
                # USBPcap completion-with-setup (stage 3) carrying IN data
                pass
            elif r.data and ctrls and ctrls[-1].is_in and not ctrls[-1].data:
                ctrls[-1].data = bytes(r.data)
    return ctrls


# ---------------------------------------------------------------- FX2 firmware


def extract_fx2_firmware(ctrls):
    """Return list of firmware images, one per RESET...RUN sequence."""
    images = []
    cur = None
    for c in ctrls:
        if c.rtype != "vendor" or c.bRequest != 0xA0 or c.is_in:
            continue
        if c.wValue == 0xE600 and len(c.data) >= 1:
            if c.data[0] & 1:  # hold 8051 in reset: a new image starts
                cur = OrderedDict()
                images.append(cur)
            else:  # release reset
                cur = None
            continue
        if cur is None:
            cur = OrderedDict()
            images.append(cur)
        cur[c.wValue] = c.data
    return images


def image_to_bin(img):
    buf = bytearray(0x10000)
    used = bytearray(0x10000)
    for addr, chunk in img.items():
        buf[addr:addr + len(chunk)] = chunk
        used[addr:addr + len(chunk)] = b"\x01" * len(chunk)
    return bytes(buf), bytes(used)


def image_to_ihex(img):
    lines = []
    for addr, chunk in img.items():
        for i in range(0, len(chunk), 16):
            part = chunk[i:i + 16]
            a = addr + i
            rec = bytes([len(part), a >> 8, a & 0xFF, 0]) + part
            csum = (-sum(rec)) & 0xFF
            lines.append(":" + rec.hex().upper() + f"{csum:02X}")
    lines.append(":00000001FF")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- reporting


def hexdump(b, width=16, limit=None):
    out = []
    n = len(b) if limit is None else min(len(b), limit)
    for i in range(0, n, width):
        chunk = b[i:i + width]
        out.append(f"{i:06x}  {chunk.hex(' '):<{width * 3}}  "
                   f"{''.join(chr(x) if 32 <= x < 127 else '.' for x in chunk)}")
    if limit is not None and len(b) > limit:
        out.append(f"... ({len(b)} bytes total)")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("capture")
    ap.add_argument("--dev", type=int, help="only this USB device address")
    ap.add_argument("--ctrl", action="store_true", help="print every control request")
    ap.add_argument("--fw-out", help="directory to write extracted FX2 firmware into")
    ap.add_argument("--bulk", type=lambda s: int(s, 0), help="endpoint to inspect, e.g. 0x86")
    ap.add_argument("--dump", type=int, default=0, help="hex-dump first N transfers of --bulk")
    ap.add_argument("--raw", help="directory: write every non-control payload as a file")
    ap.add_argument("--timeline", action="store_true", help="print a compact event timeline")
    args = ap.parse_args()

    recs = load_records(args.capture)
    if args.dev is not None:
        recs = [r for r in recs if r.dev == args.dev]
    print(f"{len(recs)} USB events")
    devs = defaultdict(int)
    for r in recs:
        devs[(r.bus, r.dev)] += 1
    print("devices seen (bus,addr): " + ", ".join(f"{b}.{d}:{n}" for (b, d), n in sorted(devs.items())))

    ctrls = pair_controls(recs)
    print(f"{len(ctrls)} control transfers ({sum(1 for c in ctrls if c.rtype == 'vendor')} vendor)")

    if args.ctrl:
        print("\n== control transfers ==")
        t0 = ctrls[0].ts if ctrls else 0
        for c in ctrls:
            dirn = "IN " if c.is_in else "OUT"
            print(f"{c.ts - t0:10.6f} dev{c.dev} {dirn} {c.rtype:6} bmRT={c.bmRequestType:02x} "
                  f"bReq={c.bRequest:02x} wValue={c.wValue:04x} wIndex={c.wIndex:04x} "
                  f"wLength={c.wLength:4d} data={len(c.data):4d} {c.describe()}")
            if c.data and not (c.rtype == "vendor" and c.bRequest == 0xA0 and len(c.data) > 64):
                print("    " + c.data.hex(" ")[:3 * 64])

    imgs = extract_fx2_firmware(ctrls)
    if imgs:
        print(f"\n== FX2 firmware: {len(imgs)} upload sequence(s) via vendor request 0xA0 ==")
        for i, img in enumerate(imgs):
            total = sum(len(v) for v in img.values())
            lo = min(img) if img else 0
            hi = max(a + len(v) for a, v in img.items()) if img else 0
            print(f"  image {i}: {len(img)} chunks, {total} bytes, address range {lo:04x}-{hi:04x}")
            if args.fw_out:
                os.makedirs(args.fw_out, exist_ok=True)
                base = os.path.join(args.fw_out, f"fx2_image{i}")
                with open(base + ".ihex", "w") as f:
                    f.write(image_to_ihex(img))
                binimg, used = image_to_bin(img)
                with open(base + ".bin", "wb") as f:
                    f.write(binimg[:hi])
                print(f"    wrote {base}.ihex and {base}.bin")
    else:
        print("\nno 0xA0 RAM uploads found (device may not be in C0-load mode in this capture)")

    # non-control traffic
    eps = defaultdict(lambda: {"n": 0, "bytes": 0, "sizes": defaultdict(int), "first": None})
    for r in recs:
        if r.xfer == 2:
            continue
        # for IN endpoints the data arrives on completion, for OUT on submit
        if (r.is_in and r.submit) or (not r.is_in and not r.submit):
            continue
        e = eps[(r.dev, r.ep, r.xfer_name)]
        e["n"] += 1
        e["bytes"] += len(r.data)
        e["sizes"][len(r.data)] += 1
        if e["first"] is None and r.data:
            e["first"] = bytes(r.data[:32])
    if eps:
        print("\n== bulk / interrupt / iso traffic ==")
        for (dev, ep, name), e in sorted(eps.items()):
            sizes = ", ".join(f"{s}x{n}" for s, n in sorted(e["sizes"].items(), key=lambda x: -x[1])[:6])
            print(f"  dev{dev} ep{ep:02x} {name:4} {'IN ' if ep & 0x80 else 'OUT'}: {e['n']} transfers, "
                  f"{e['bytes']} bytes; sizes {sizes}")
            if e["first"]:
                print(f"      first bytes: {e['first'].hex(' ')}")

    if args.bulk is not None and args.dump:
        n = 0
        for r in recs:
            if r.ep != args.bulk or r.xfer == 2 or not r.data:
                continue
            if (r.is_in and r.submit) or (not r.is_in and not r.submit):
                continue
            print(f"\n-- ep{r.ep:02x} transfer #{n} t={r.ts:.6f} {len(r.data)} bytes")
            print(hexdump(r.data, limit=256))
            n += 1
            if n >= args.dump:
                break

    if args.raw:
        os.makedirs(args.raw, exist_ok=True)
        n = 0
        for r in recs:
            if r.xfer == 2 or not r.data:
                continue
            if (r.is_in and r.submit) or (not r.is_in and not r.submit):
                continue
            with open(os.path.join(args.raw, f"{n:06d}_ep{r.ep:02x}_{r.xfer_name}.bin"), "wb") as f:
                f.write(r.data)
            n += 1
        print(f"wrote {n} payload files to {args.raw}")

    if args.timeline:
        print("\n== timeline ==")
        t0 = recs[0].ts if recs else 0
        last = None
        for r in recs:
            if r.xfer == 2 and not (r.submit and r.setup):
                continue
            if (r.xfer != 2) and ((r.is_in and r.submit) or (not r.is_in and not r.submit)):
                continue
            key = (r.xfer, r.ep, len(r.data))
            if key == last:
                continue  # collapse repeats
            last = key
            if r.xfer == 2:
                s = struct.unpack("<BBHHH", r.setup)
                print(f"{r.ts - t0:10.6f} ctrl bmRT={s[0]:02x} bReq={s[1]:02x} wV={s[2]:04x} wI={s[3]:04x} wL={s[4]}")
            else:
                print(f"{r.ts - t0:10.6f} {r.xfer_name} ep{r.ep:02x} {len(r.data)} bytes")


if __name__ == "__main__":
    main()
