"""
Cypress FX2/FX2LP RAM firmware loader (host side), using pyusb.

Protocol (EZ-USB TRM ch. 3): vendor request 0xA0 reads/writes 8051 code RAM
(0x0000-0x3FFF on FX2LP, plus scratch 0xE000-0xE1FF and registers). Writing
0x01 to CPUCS (0xE600) holds the CPU in reset, 0x00 releases it. After the
release the firmware typically disconnects and re-enumerates ("ReNumeration")
with its own VID/PID/descriptors.
"""
import time

import usb.core
import usb.util

VID = 0x2AA9
PID_BARE = 0x9F00
CPUCS = 0xE600
REQ_RAM = 0xA0
CHUNK = 4096  # Cypress and fxload both use <= 4 KiB per control transfer


def parse_ihex(text):
    """Intel HEX -> dict {address: bytes} (contiguous records merged)."""
    chunks = {}
    base = 0
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(":"):
            continue
        raw = bytes.fromhex(line[1:])
        n, addr, rtype = raw[0], (raw[1] << 8) | raw[2], raw[3]
        data = raw[4:4 + n]
        if (sum(raw) & 0xFF) != 0:
            raise ValueError(f"ihex checksum error: {line}")
        if rtype == 0:
            a = base + addr
            # merge with previous contiguous chunk
            prev = None
            for pa, pd in chunks.items():
                if pa + len(pd) == a:
                    prev = pa
                    break
            if prev is not None:
                chunks[prev] += data
            else:
                chunks[a] = bytearray(data)
        elif rtype == 1:
            break
        elif rtype == 2:
            base = ((raw[4] << 8) | raw[5]) << 4
        elif rtype == 4:
            base = ((raw[4] << 8) | raw[5]) << 16
    return {a: bytes(d) for a, d in chunks.items()}


def load_bin(data, origin=0):
    return {origin: bytes(data)}


def find_bare_device():
    return usb.core.find(idVendor=VID, idProduct=PID_BARE)


def ram_write(dev, addr, data):
    for off in range(0, len(data), CHUNK):
        part = data[off:off + CHUNK]
        n = dev.ctrl_transfer(0x40, REQ_RAM, addr + off, 0, part, timeout=2000)
        if n != len(part):
            raise IOError(f"short RAM write at {addr + off:04x}: {n}/{len(part)}")


def ram_read(dev, addr, n):
    return bytes(dev.ctrl_transfer(0xC0, REQ_RAM, addr, 0, n, timeout=2000))


def cpu_reset(dev, hold):
    dev.ctrl_transfer(0x40, REQ_RAM, CPUCS, 0, bytes([1 if hold else 0]), timeout=2000)


def load_firmware(dev, image, verify=False):
    """
    Upload {addr: bytes} into FX2 RAM and start the CPU. `image` comes from
    parse_ihex() or load_bin(). Returns after the CPU is released; the device
    will usually disconnect immediately afterwards.
    """
    cpu_reset(dev, True)
    for addr in sorted(image):
        data = image[addr]
        # Only internal code RAM (0x0000-0x3FFF) and scratch (0xE000-0xE1FF)
        # are writable through 0xA0 while in reset; everything else is skipped.
        if not (addr < 0x4000 or 0xE000 <= addr < 0xE200):
            continue
        ram_write(dev, addr, data)
        if verify:
            back = ram_read(dev, addr, len(data))
            if back != data:
                raise IOError(f"verify failed at {addr:04x}")
    try:
        cpu_reset(dev, False)
    except usb.core.USBError:
        pass  # the device may drop off the bus before the status stage


def wait_for_device(vid=VID, pid=None, timeout=10.0, exclude_pid=PID_BARE):
    """Wait for the re-enumerated (firmware-running) device to appear."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        for d in usb.core.find(find_all=True, idVendor=vid):
            if pid is not None and d.idProduct != pid:
                continue
            if pid is None and d.idProduct == exclude_pid:
                continue
            return d
        time.sleep(0.2)
    return None
