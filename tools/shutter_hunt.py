#!/usr/bin/env python3
"""
shutter_hunt.py - pulse candidate control lines while streaming and watch for a shutter
event (frame becomes nearly uniform: spatial std collapses; or mean jumps).

  shutter_hunt.py pa            # pulse PA0..PA7 low and high (50 ms, then 500 ms)
  shutter_hunt.py ctl           # try CTL0..2 level combinations
  shutter_hunt.py watch -t 30   # just monitor stats for t seconds (autonomous shutter?)
  shutter_hunt.py pin A 0 --level 0 --ms 500   # one specific pulse
"""
import argparse
import sys
import time

import numpy as np

sys.path.insert(0, "/home/alexandercrain/Repositories/ICI9320P")
from ici9320 import ICI9320, protocol as P  # noqa: E402


def stats(frame):
    f = frame.astype(np.float32)
    return float(f.mean()), float(f.std())


class Hunter:
    def __init__(self, cam):
        self.cam = cam
        self.log = []

    def drain(self, n, label=""):
        out = []
        for fr in self.cam.frames(n, timeout=1.0):
            m, s = stats(fr)
            out.append((time.time(), m, s))
        return out

    def baseline(self):
        b = self.drain(30)
        self.mean0 = np.mean([m for _, m, _ in b]); self.std0 = np.mean([s for _, _, s in b])
        print(f"baseline: mean {self.mean0:.1f} spatial-std {self.std0:.2f}")

    def check(self, samples, tag):
        hits = [(t, m, s) for t, m, s in samples if s < 0.35 * self.std0 or abs(m - self.mean0) > 25]
        flag = "  <== EVENT" if hits else ""
        worst = min(samples, key=lambda x: x[2]) if samples else (0, 0, 0)
        print(f"{time.strftime('%H:%M:%S')} {tag:40s} frames={len(samples):3d} min-std={worst[2]:6.2f} "
              f"mean-range={min(m for _, m, _ in samples):.0f}-{max(m for _, m, _ in samples):.0f}{flag}")
        self.log.append((tag, bool(hits)))
        return bool(hits)

    def pulse_port(self, port, bit, level, ms):
        idx = "ABCDE".index(port)
        val = (1 << bit) if level else 0
        self.cam.vendor_out(P.VR_PORT_WRITE, idx, val)
        self.cam.vendor_out(P.VR_OE_WRITE, idx, 1 << bit)      # drive
        time.sleep(ms / 1000.0)
        self.cam.vendor_out(P.VR_OE_WRITE, idx, 0)              # release (input again)

    def pin_test(self, port, bit, level, ms, observe=90):
        self.drain(5)
        self.pulse_port(port, bit, level, ms)
        s = self.drain(observe)
        return self.check(s, f"P{port}{bit} driven {'HIGH' if level else 'LOW'} for {ms} ms")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pa")
    sub.add_parser("ctl")
    p = sub.add_parser("watch"); p.add_argument("-t", type=float, default=30)
    p = sub.add_parser("pin"); p.add_argument("port"); p.add_argument("bit", type=int)
    p.add_argument("--level", type=int, default=0); p.add_argument("--ms", type=int, default=500)
    args = ap.parse_args()

    with ICI9320() as cam:
        cam.start_stream(n_transfers=12)
        h = Hunter(cam)
        h.baseline()
        pins = cam.vendor_in(P.VR_READ_PINS, 0, 0, 8)
        print(f"PA={pins[0]:08b} PC={pins[2]:08b} PE={pins[4]:08b} RDY={pins[5]:06b}")
        if args.cmd == "watch":
            t0 = time.time()
            while time.time() - t0 < args.t:
                h.check(h.drain(60), "watch")
        elif args.cmd == "pin":
            h.pin_test(args.port, args.bit, args.level, args.ms, observe=120)
        elif args.cmd == "pa":
            for bit in range(8):
                for level in (0, 1):
                    for ms in (50, 500):
                        h.pin_test("A", bit, level, ms)
            print("\nPA events:", [t for t, hit in h.log if hit] or "none")
        elif args.cmd == "ctl":
            # CTL levels apply to the GPIF outputs; stream must be restarted to rebuild the waveform
            for levels in (0x3F, 0x3E, 0x3D, 0x3B, 0x00, 0x01, 0x02, 0x04, 0x3F):
                cam.stop_stream()
                cam.vendor_out(P.VR_SET_CTL, levels, 0)
                cam.start_stream(n_transfers=12)
                h.drain(5)
                s = h.drain(90)
                h.check(s, f"CTL levels = {levels:06b}")
            print("\nCTL events:", [t for t, hit in h.log if hit] or "none")
        cam.stop_stream()


if __name__ == "__main__":
    main()
