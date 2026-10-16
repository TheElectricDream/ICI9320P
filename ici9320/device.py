"""
ICI 9320P host driver (reverse engineered).

Data path: the camera's FX2LP runs our firmware (ici9320/firmware/ici9320.ihx) as a GPIF
master that samples the sensor's 16-bit bus on every RDY1 strobe while RDY0 (frame
valid) is high, and marks every frame boundary with a short or zero-length USB packet.
Frames are 320x240 little-endian 16-bit words holding 14-bit raw counts.

Works on x86 and ARM Linux (python-libusb1 + numpy only).
"""
import os
import struct
import threading
import time
from collections import deque

import usb1

from . import protocol as P

FIRMWARE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "firmware", "ici9320.ihx")


def _parse_ihex(text):
    chunks = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(":"):
            continue
        raw = bytes.fromhex(line[1:])
        n, addr, rtype = raw[0], (raw[1] << 8) | raw[2], raw[3]
        if rtype == 0:
            chunks[addr] = raw[4:4 + n]
        elif rtype == 1:
            break
    # merge contiguous
    out = {}
    for a in sorted(chunks):
        d = chunks[a]
        if out and max(out) + len(out[max(out)]) == a:
            out[max(out)] += d
        else:
            out[a] = bytearray(d)
    return {a: bytes(d) for a, d in out.items()}


class ICI9320:
    """Open the camera, load firmware if needed, stream frames."""

    def __init__(self, firmware=FIRMWARE, timeout_ms=2000):
        self.firmware = firmware
        self.timeout = timeout_ms
        self.ctx = usb1.USBContext()
        self.handle = None
        self._rx = None

    # ------------------------------------------------------------ open/close
    def __enter__(self):
        return self.open()

    def __exit__(self, *a):
        self.close()

    def _find(self, pid):
        for d in self.ctx.getDeviceList(skip_on_error=True):
            if d.getVendorID() == P.VID and d.getProductID() == pid:
                return d
        return None

    def open(self):
        dev = self._find(P.PID_FW)
        if dev is None:
            bare = self._find(P.PID_BARE)
            if bare is None:
                raise RuntimeError("no ICI 9320P found (2aa9:9f00 or 2aa9:9f10)")
            self._load_firmware(bare)
            dev = self._wait(P.PID_FW, 10.0)
            if dev is None:
                raise RuntimeError("camera did not re-enumerate after firmware load")
            time.sleep(0.3)
        self.handle = dev.open()
        self.handle.claimInterface(0)
        v = self.vendor_in(P.VR_VERSION, 0, 0, 4)
        if v[:2] != b"XP":
            raise RuntimeError(f"unexpected firmware version reply {v.hex()}")
        self.fw_version = (v[2], v[3])
        return self

    def close(self):
        if self._rx:
            self.stop_stream()
        if self.handle:
            try:
                self.handle.releaseInterface(0)
            except usb1.USBError:
                pass
            self.handle.close()
            self.handle = None

    def _wait(self, pid, timeout):
        t0 = time.time()
        while time.time() - t0 < timeout:
            d = self._find(pid)
            if d is not None:
                return d
            time.sleep(0.2)
        return None

    def _load_firmware(self, dev):
        with open(self.firmware) as f:
            image = _parse_ihex(f.read())
        h = dev.open()
        try:
            h.controlWrite(0x40, P.VR_FX2_RAM, 0xE600, 0, b"\x01", 2000)
            for addr in sorted(image):
                data = image[addr]
                if not (addr < 0x4000 or 0xE000 <= addr < 0xE200):
                    continue
                for off in range(0, len(data), 4096):
                    h.controlWrite(0x40, P.VR_FX2_RAM, addr + off, 0, data[off:off + 4096], 2000)
            try:
                h.controlWrite(0x40, P.VR_FX2_RAM, 0xE600, 0, b"\x00", 2000)
            except usb1.USBError:
                pass
        finally:
            try:
                h.close()
            except usb1.USBError:
                pass

    # ------------------------------------------------------------ control
    def vendor_in(self, req, value=0, index=0, length=64, timeout=None):
        return bytes(self.handle.controlRead(0xC0, req, value, index, length, timeout or self.timeout))

    def vendor_out(self, req, value=0, index=0, data=b"", timeout=None):
        return self.handle.controlWrite(0x40, req, value, index, bytes(data), timeout or self.timeout)

    def reload_firmware(self):
        """Push the current firmware file through the resident loader (no replug)."""
        with open(self.firmware) as f:
            image = _parse_ihex(f.read())
        self.vendor_out(P.VR_FX2_RAM, 0xE600, 0, b"\x01")
        for addr in sorted(image):
            data = image[addr]
            if not (addr < 0x4000 or 0xE000 <= addr < 0xE200):
                continue
            for off in range(0, len(data), 4096):
                self.vendor_out(P.VR_FX2_RAM, addr + off, 0, data[off:off + 4096])
        try:
            self.vendor_out(P.VR_FX2_RAM, 0xE600, 0, b"\x00", timeout=500)
        except usb1.USBError:
            pass
        self.handle.close(); self.handle = None
        time.sleep(0.5)
        dev = self._wait(P.PID_FW, 10.0)
        if dev is None:
            raise RuntimeError("camera did not come back after reload")
        time.sleep(0.3)
        self.handle = dev.open()
        self.handle.claimInterface(0)

    # ------------------------------------------------------------ sensors
    def temperature_c(self):
        """Housing temperature from the MCP98243 on the camera's I2C bus (°C)."""
        d = self.vendor_in(P.VR_I2C_READ, P.I2C_TEMP, 0x05, 2)
        raw = ((d[0] & 0x1F) << 8) | d[1]
        if d[0] & 0x10:
            raw -= 8192
        return raw / 16.0

    def status(self):
        d = self.vendor_in(P.VR_STREAM_STATUS, 0, 0, 18)
        return dict(ifconfig=d[0], gpiftrig=d[1], rdy=d[3], ep6cs=d[5], ep6flags=d[8],
                    frames=d[16] | (d[17] << 8))

    # ------------------------------------------------------------ streaming
    def start_stream(self, n_transfers=8, transfer_bytes=P.FRAME_BYTES):
        """Start continuous frame streaming with an asynchronous libusb reader."""
        if self._rx:
            return
        self.vendor_out(P.VR_STREAM_STOP)
        self._rx = _AsyncReader(self.ctx, self.handle, P.EP_VIDEO, n_transfers, transfer_bytes)
        self._rx.start()
        flags = P.F_WORDWIDE | P.F_INTCLK | P.F_48MHZ
        self.vendor_out(P.VR_STREAM_START, P.MODE_FRAMES, flags)

    def stop_stream(self):
        if self._rx:
            try:
                self.vendor_out(P.VR_STREAM_STOP, timeout=500)
            except usb1.USBError:
                pass
            self._rx.stop()
            self._rx = None

    def read_frame(self, timeout=1.0):
        """Return the next frame as bytes (153600 bytes when complete) or None on timeout."""
        return self._rx.get_frame(timeout) if self._rx else None

    def frames(self, n=None, timeout=1.0, skip_shutter=True, lookahead=8):
        """
        Generator of numpy (240,320) uint16 arrays of raw 14-bit counts.
        Incomplete frames are dropped. The camera performs an autonomous flat-field
        correction (shutter) every ~30 s while warming up and every ~2 min when stable.
        Its only unambiguous signature is one or more sentinel frames in which every pixel
        has the same value; the paddle closes during the ~6 frames before them and the
        offset table changes after them. With skip_shutter=True frames are delayed by
        `lookahead` frames (8 = 133 ms) so the closing frames can be dropped retroactively;
        the closed-paddle frame and the housing temperature are kept in self.last_shutter.
        """
        import numpy as np
        pending = deque()
        i = 0
        while n is None or i < n:
            f = self.read_frame(timeout)
            if f is None:
                return
            if len(f) != P.FRAME_BYTES:
                self._rx.incomplete += 1
                continue
            fr = np.frombuffer(f, dtype="<u2").reshape(P.HEIGHT, P.WIDTH)
            kind = self.classify_frame(fr)
            if not skip_shutter:
                yield fr
                i += 1
                continue
            if kind == "flat":
                # paddle was closed in the frames just before: keep the most uniform as reference
                if pending:
                    ref = min(pending, key=lambda x: float(x.std()))
                    try:
                        t = self.temperature_c()
                    except Exception:
                        t = None
                    self.last_shutter = dict(frame=ref.copy(), temperature_c=t, time=time.time(),
                                             mean=float(ref.mean()))
                pending.clear()
                continue
            if kind == "settling":
                continue
            pending.append(fr)
            if len(pending) > lookahead:
                yield pending.popleft()
                i += 1
        # (frames still pending at the end are dropped: they could not be verified)

    # ------------------------------------------------------------ shutter detection
    _settle = 0
    shutter_events = 0
    last_shutter = None     # dict(frame=np.ndarray, temperature_c=float, time=float, mean=float)

    def classify_frame(self, fr):
        """'flat' for a sentinel frame (all pixels equal, emitted while the shutter is closed),
        'settling' for the 10 frames after it (paddle re-opening, offset table changed),
        otherwise 'ok'. Scene statistics are deliberately not used: a warm hand filling the
        field of view looks exactly like the closed paddle."""
        if int(fr.min()) == int(fr.max()):
            if self._settle == 0:
                self.shutter_events += 1
            self._settle = 10
            return "flat"
        if self._settle:
            self._settle -= 1
            return "settling"
        return "ok"


class _AsyncReader:
    """Keeps n bulk IN transfers queued so the FX2 FIFO never starves; splits frames on short packets."""

    def __init__(self, ctx, handle, ep, n, size):
        self.ctx, self.handle, self.ep, self.n, self.size = ctx, handle, ep, n, size
        self.frames = deque()
        self.cv = threading.Condition()
        self.cur = bytearray()
        self.total_bytes = self.total_frames = self.incomplete = self.errors = 0
        self.running = False
        self.transfers = []

    def _cb(self, xfer):
        st = xfer.getStatus()
        if st == usb1.TRANSFER_COMPLETED:
            n = xfer.getActualLength()
            data = xfer.getBuffer()[:n]
            self.total_bytes += n
            with self.cv:
                self.cur += data
                # a short (or zero) packet ends the frame; transfers are frame-sized so any
                # actual length < requested means the device delimited a frame
                if n < self.size or n == 0 or len(self.cur) >= P.FRAME_BYTES:
                    if self.cur:
                        self.frames.append(bytes(self.cur))
                        self.total_frames += 1
                        self.cur = bytearray()
                        self.cv.notify()
        elif st not in (usb1.TRANSFER_CANCELLED,):
            self.errors += 1
        if self.running:
            try:
                xfer.submit()
            except usb1.USBError:
                self.errors += 1

    def start(self):
        self.running = True
        for _ in range(self.n):
            t = self.handle.getTransfer()
            t.setBulk(self.ep, self.size, callback=self._cb, timeout=0)
            t.submit()
            self.transfers.append(t)
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while self.running:
            try:
                self.ctx.handleEventsTimeout(0.1)
            except usb1.USBError:
                pass

    def stop(self):
        self.running = False
        for t in self.transfers:
            try:
                if t.isSubmitted():
                    t.cancel()
            except usb1.USBError:
                pass
        deadline = time.time() + 1.0
        while any(t.isSubmitted() for t in self.transfers) and time.time() < deadline:
            try:
                self.ctx.handleEventsTimeout(0.05)
            except usb1.USBError:
                pass
        self.thread.join(timeout=1.0)
        for t in self.transfers:
            t.close()
        self.transfers = []

    def get_frame(self, timeout):
        with self.cv:
            if not self.frames:
                self.cv.wait(timeout)
            return self.frames.popleft() if self.frames else None
