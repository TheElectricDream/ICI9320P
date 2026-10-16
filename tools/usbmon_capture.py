#!/usr/bin/env python3
"""
usbmon_capture.py - record raw USB traffic from the Linux usbmon binary
interface into a .pcap file (linktype 220, readable by Wireshark and
tools/usbcap.py). No dependencies, so it also works on the Jetson.

Must run as root:
  sudo modprobe usbmon
  sudo python3 tools/usbmon_capture.py --bus 1 -o captures/irflash.pcap

Bus 0 captures all buses. Stop with Ctrl-C.

Typical use: run a Windows VM (QEMU) with the camera passed through, start
this capture *before* the camera is attached, then start IR Flash in the VM.
Full payloads are kept (unlike the text interface which truncates to 32 bytes),
which is what lets usbcap.py reconstruct the FX2 firmware upload.
"""
import argparse
import os
import signal
import struct
import sys
import time

HDR_LEN = 64            # struct mon_bin_hdr
MAX_EVENT = 64 + (4 << 20)  # usbmon ring default is 300 KiB; allow large anyway
MON_IOCT_RING_SIZE = 0x40089204  # _IOW('9', 4, int)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bus", type=int, default=0, help="USB bus number (0 = all)")
    ap.add_argument("-o", "--out", required=True, help="output .pcap file")
    ap.add_argument("--dev", type=int, help="only keep events for this device address")
    args = ap.parse_args()

    node = f"/dev/usbmon{args.bus}"
    if not os.path.exists(node):
        sys.exit(f"{node} missing: run 'sudo modprobe usbmon' (and run this as root)")
    fd = os.open(node, os.O_RDONLY)
    try:
        import fcntl
        fcntl.ioctl(fd, MON_IOCT_RING_SIZE, struct.pack("i", 8 << 20))
    except Exception as e:  # not fatal
        print(f"(could not enlarge usbmon ring: {e})", file=sys.stderr)

    out = open(args.out, "wb")
    # pcap global header: magic, v2.4, tz 0, sigfigs 0, snaplen, linktype 220
    out.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, MAX_EVENT, 220))
    n = 0
    stop = False

    def on_sig(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGINT, on_sig)
    signal.signal(signal.SIGTERM, on_sig)

    print(f"capturing {node} -> {args.out} (Ctrl-C to stop)", file=sys.stderr)
    while not stop:
        try:
            ev = os.read(fd, MAX_EVENT)
        except InterruptedError:
            continue
        if len(ev) < HDR_LEN:
            continue
        devnum = ev[11]
        if args.dev is not None and devnum != args.dev:
            continue
        ts_sec, ts_usec = struct.unpack_from("<qi", ev, 16)
        out.write(struct.pack("<IIII", ts_sec & 0xFFFFFFFF, ts_usec, len(ev), len(ev)))
        out.write(ev)
        n += 1
        if n % 500 == 0:
            out.flush()
            print(f"\r{n} events", end="", file=sys.stderr)
    out.close()
    print(f"\nsaved {n} events to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
