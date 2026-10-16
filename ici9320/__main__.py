"""python3 -m ici9320 {info|grab|png} ..."""
import argparse
import sys
import time

from .device import ICI9320
from . import protocol as P


def main():
    ap = argparse.ArgumentParser(prog="ici9320")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", help="firmware version, housing temperature, stream status")
    p = sub.add_parser("grab", help="stream N frames, report completeness and fps, optionally save raw")
    p.add_argument("-n", type=int, default=120); p.add_argument("-o", "--out")
    p.add_argument("--transfers", type=int, default=8)
    p = sub.add_parser("png", help="grab one frame and save a stretched PNG")
    p.add_argument("-o", "--out", default="frame.png"); p.add_argument("--avg", type=int, default=1)
    sub.add_parser("reload", help="reload the packaged firmware through the resident loader")
    p = sub.add_parser("watch", help="stream and report shutter (FFC) events as they happen")
    p.add_argument("-t", type=float, default=300)
    args = ap.parse_args()

    with ICI9320() as cam:
        if args.cmd == "info":
            print(f"firmware {cam.fw_version}, housing temperature {cam.temperature_c():.2f} °C, status {cam.status()}")
        elif args.cmd == "reload":
            cam.reload_firmware(); print("reloaded", cam.vendor_in(P.VR_VERSION, 0, 0, 4))
        elif args.cmd == "grab":
            cam.start_stream(n_transfers=args.transfers)
            t0 = time.time(); got = []; sizes = {}
            while len(got) < args.n:
                f = cam.read_frame(1.0)
                if f is None:
                    print("timeout"); break
                sizes[len(f)] = sizes.get(len(f), 0) + 1
                if len(f) == P.FRAME_BYTES:
                    got.append(f)
            dt = time.time() - t0
            st = cam.stream_stats
            cam.stop_stream()
            total = sum(sizes.values())
            print(f"{len(got)} complete / {total} frames in {dt:.2f}s -> {total/dt:.1f} fps; sizes {sizes}; stats {st}")
            if args.out and got:
                with open(args.out, "wb") as fh:
                    for f in got:
                        fh.write(f)
                print("saved", args.out)
        elif args.cmd == "watch":
            import numpy as np
            cam.start_stream()
            t0 = time.time(); last = None; n = 0
            while time.time() - t0 < args.t:
                f = cam.read_frame(1.0)
                if f is None or len(f) != P.FRAME_BYTES:
                    continue
                fr = np.frombuffer(f, dtype="<u2").reshape(P.HEIGHT, P.WIDTH)
                kind = cam.classify_frame(fr); n += 1
                if kind != "ok" and kind != last:
                    print(f"{time.strftime('%H:%M:%S')} t={time.time()-t0:7.2f}s frame {n}: {kind:8s} mean {fr.mean():.1f} std {fr.std():.2f}")
                last = kind
            print(f"{n} frames, {cam.shutter_events} shutter events; last closed-paddle mean "
                  f"{cam.last_shutter['mean']:.1f} at housing {cam.last_shutter['temperature_c']} °C" if cam.last_shutter else f"{n} frames, no shutter events")
            cam.stop_stream()
        elif args.cmd == "png":
            import numpy as np
            from PIL import Image
            cam.start_stream()
            acc = None; k = 0
            for fr in cam.frames(args.avg, timeout=2.0):
                acc = fr.astype(np.float32) if acc is None else acc + fr
                k += 1
            cam.stop_stream()
            if acc is None:
                sys.exit("no frame")
            img = acc / k
            lo, hi = np.percentile(img, 0.5), np.percentile(img, 99.5)
            Image.fromarray(np.clip((img - lo) / max(hi - lo, 1) * 255, 0, 255).astype(np.uint8)).save(args.out)
            print(f"saved {args.out} (avg of {k}, raw {img.min():.0f}-{img.max():.0f}, housing {cam.temperature_c():.2f} °C)")


if __name__ == "__main__":
    main()
