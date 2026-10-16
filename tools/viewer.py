#!/usr/bin/env python3
"""
viewer.py - live window for the ICI 9320P.

  python3 tools/viewer.py              # 3x scaled window, auto contrast
  python3 tools/viewer.py --scale 2 --palette iron
  python3 tools/viewer.py --headless 30 -o /tmp/x.png   # no window, render 30 frames, save last

Keys:  q/Esc quit   s snapshot (PNG + raw .bin in captures/)   p next palette
       a toggle auto-contrast / frozen range   [ ] widen / narrow contrast when frozen
       h toggle histogram-equalised view       r reset
Shutter (flat-field) frames are detected and the last good frame is held meanwhile.
"""
import argparse
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from ici9320 import ICI9320, protocol as P  # noqa: E402

PALETTES = ["gray", "iron", "rainbow", "inverted"]


def iron_lut():
    # classic "ironbow": black -> purple -> red -> orange -> yellow -> white
    stops = [(0, (0, 0, 0)), (0.15, (32, 0, 96)), (0.35, (140, 0, 160)), (0.55, (220, 50, 40)),
             (0.75, (255, 150, 0)), (0.9, (255, 230, 60)), (1.0, (255, 255, 255))]
    lut = np.zeros((256, 3), np.uint8)
    for i in range(256):
        x = i / 255
        for (x0, c0), (x1, c1) in zip(stops, stops[1:]):
            if x0 <= x <= x1:
                t = (x - x0) / (x1 - x0)
                lut[i] = [int(c0[k] + t * (c1[k] - c0[k])) for k in range(3)]
                break
    return lut


def rainbow_lut():
    lut = np.zeros((256, 3), np.uint8)
    for i in range(256):
        x = i / 255 * 4
        r = np.clip(min(x - 1.5, -x + 4.5), 0, 1); g = np.clip(min(x - 0.5, -x + 3.5), 0, 1)
        b = np.clip(min(x + 0.5, -x + 2.5), 0, 1)
        lut[i] = (int(r * 255), int(g * 255), int(b * 255))
    return lut


LUTS = {"gray": np.stack([np.arange(256)] * 3, 1).astype(np.uint8), "iron": iron_lut(),
        "rainbow": rainbow_lut(), "inverted": np.stack([255 - np.arange(256)] * 3, 1).astype(np.uint8)}


class Renderer:
    def __init__(self, palette="gray", scale=3):
        self.palette, self.scale = palette, scale
        self.auto, self.lo, self.hi, self.equalize = True, 5000.0, 5500.0, False

    def render(self, fr):
        f = fr.astype(np.float32)
        if self.auto:
            lo, hi = np.percentile(f, 0.5), np.percentile(f, 99.5)
            if hi - lo < 20:
                hi = lo + 20
            # smooth the range so the picture does not flicker
            self.lo += 0.2 * (lo - self.lo); self.hi += 0.2 * (hi - self.hi)
        if self.equalize:
            hist, edges = np.histogram(f, 256, (self.lo, self.hi))
            cdf = np.cumsum(hist).astype(np.float32); cdf /= max(cdf[-1], 1)
            idx = np.clip(((f - self.lo) / max(self.hi - self.lo, 1) * 255).astype(int), 0, 255)
            img8 = (cdf[idx] * 255).astype(np.uint8)
        else:
            img8 = np.clip((f - self.lo) / max(self.hi - self.lo, 1) * 255, 0, 255).astype(np.uint8)
        rgb = LUTS[self.palette][img8]
        if self.scale != 1:
            rgb = rgb.repeat(self.scale, 0).repeat(self.scale, 1)
        return rgb


def run_headless(n, out, palette, scale):
    from PIL import Image
    r = Renderer(palette, scale)
    with ICI9320() as cam:
        cam.start_stream()
        rgb = None; k = 0; t0 = time.time()
        for fr in cam.frames(n, timeout=2.0):
            rgb = r.render(fr); k += 1
        dt = time.time() - t0
        cam.stop_stream()
    if rgb is None:
        sys.exit("no frames")
    Image.fromarray(rgb).save(out)
    print(f"rendered {k} frames in {dt:.2f}s ({k/dt:.1f} fps incl. render), range {r.lo:.0f}-{r.hi:.0f}, saved {out}")


def run_window(palette, scale):
    import tkinter as tk
    from PIL import Image
    r = Renderer(palette, scale)
    cam = ICI9320().open()
    cam.start_stream()
    root = tk.Tk(); root.title("ICI 9320P live")
    canvas = tk.Canvas(root, width=P.WIDTH * scale, height=P.HEIGHT * scale, highlightthickness=0, bg="black")
    canvas.pack()
    status = tk.Label(root, anchor="w", font=("monospace", 10)); status.pack(fill="x")
    state = dict(last=None, frames=0, t0=time.time(), fps=0.0, shown=0, tshown=time.time(), tshutter=0.0,
                 temp=cam.temperature_c(), ttemp=time.time(), photo=None, running=True, raw=None)

    def key(ev):
        k = ev.keysym.lower()
        if k in ("q", "escape"):
            state["running"] = False; root.destroy()
        elif k == "p":
            r.palette = PALETTES[(PALETTES.index(r.palette) + 1) % len(PALETTES)]
        elif k == "a":
            r.auto = not r.auto
        elif k == "h":
            r.equalize = not r.equalize
        elif k == "r":
            r.auto, r.equalize = True, False
        elif k == "bracketleft" and not r.auto:
            c = (r.lo + r.hi) / 2; w = (r.hi - r.lo) * 1.25; r.lo, r.hi = c - w / 2, c + w / 2
        elif k == "bracketright" and not r.auto:
            c = (r.lo + r.hi) / 2; w = (r.hi - r.lo) / 1.25; r.lo, r.hi = c - w / 2, c + w / 2
        elif k == "s" and state["raw"] is not None:
            os.makedirs("captures", exist_ok=True)
            base = time.strftime("captures/snap_%Y%m%d_%H%M%S")
            Image.fromarray(r.render(state["raw"])).save(base + ".png")
            state["raw"].astype("<u2").tofile(base + ".bin")
            print("saved", base + ".png / .bin")
    root.bind("<Key>", key)

    def tick():
        if not state["running"]:
            return
        # drain everything that arrived, keep the newest good frame
        newest = None
        while True:
            f = cam.read_frame(0.0 if newest is not None else 0.2)
            if f is None:
                break
            if len(f) != P.FRAME_BYTES:
                continue
            fr = np.frombuffer(f, dtype="<u2").reshape(P.HEIGHT, P.WIDTH)
            kind = cam.classify_frame(fr); state["frames"] += 1
            if kind == "flat":
                state["tshutter"] = time.time()   # sentinel frame: hold the picture, flag the event
            else:
                newest = fr                       # settling frames are real pictures, show them
        if newest is not None:
            state["raw"] = newest
            rgb = r.render(newest)
            # plain tkinter PhotoImage from a binary PPM: no Pillow-Tk bridge needed
            state["photo"] = tk.PhotoImage(data=b"P6 %d %d 255\n" % (rgb.shape[1], rgb.shape[0]) + rgb.tobytes())
            canvas.delete("all"); canvas.create_image(0, 0, anchor="nw", image=state["photo"])
            state["shown"] += 1
        now = time.time()
        if now - state["tshown"] >= 1.0:
            state["fps"] = state["shown"] / (now - state["tshown"]); state["shown"] = 0; state["tshown"] = now
        if now - state["ttemp"] >= 2.0:
            try:
                state["temp"] = cam.temperature_c()
            except Exception:
                pass
            state["ttemp"] = now
        raw = state["raw"]
        status.config(text=(f"{state['fps']:4.1f} fps shown | raw {int(raw.min()) if raw is not None else 0}-"
                            f"{int(raw.max()) if raw is not None else 0} | range {r.lo:.0f}-{r.hi:.0f} "
                            f"{'auto' if r.auto else 'frozen'}{' eq' if r.equalize else ''} | {r.palette} | "
                            f"housing {state['temp']:.2f} °C | shutter events {cam.shutter_events} "
                            f"{'[SHUTTER]' if now - state['tshutter'] < 1.0 else ''} | q quit s snap p palette a auto h eq"))
        root.after(5, tick)

    root.after(10, tick)
    try:
        root.mainloop()
    finally:
        try:
            cam.stop_stream(); cam.close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scale", type=int, default=3)
    ap.add_argument("--palette", choices=PALETTES, default="gray")
    ap.add_argument("--headless", type=int, metavar="N", help="render N frames without a window")
    ap.add_argument("-o", "--out", default="viewer_test.png")
    args = ap.parse_args()
    if args.headless:
        run_headless(args.headless, args.out, args.palette, args.scale)
    else:
        run_window(args.palette, args.scale)


if __name__ == "__main__":
    main()
