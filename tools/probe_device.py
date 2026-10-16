#!/usr/bin/env python3
"""
probe_device.py - identify what state the ICI 9320P is in and what it answers to.

Safe, read-only probing (no EEPROM writes, no firmware upload):
  1. find 2aa9:* devices, print descriptors / speed
  2. test whether the Cypress FX2 boot ROM answers vendor request 0xA0
     (RAM read) -> confirms "C0 load, waiting for firmware" state
  3. optionally (--eeprom) read the boot EEPROM through whitequark's fx2 RAM
     bootloader (loads a small firmware into RAM; a power cycle undoes it)
  4. optionally (--poke) select alt setting 1 and try short bulk reads on the
     IN endpoints to see whether anything streams without firmware (expect no)

Requires the udev rule from udev/99-ici-9320p.rules (or run with sudo).
"""
import argparse
import sys
import time

import usb.core
import usb.util

VID = 0x2AA9


def find_all():
    return list(usb.core.find(find_all=True, idVendor=VID))


def show(dev):
    print(f"Bus {dev.bus:03d} Device {dev.address:03d}: {dev.idVendor:04x}:{dev.idProduct:04x} "
          f"bcdDevice={dev.bcdDevice:04x} class={dev.bDeviceClass:02x} "
          f"speed={ {1:'low',2:'full',3:'high',4:'super'}.get(dev.speed,'?') }")
    for i in ("iManufacturer", "iProduct", "iSerialNumber"):
        idx = getattr(dev, i)
        if idx:
            try:
                print(f"  {i}: {usb.util.get_string(dev, idx)!r}")
            except Exception as e:
                print(f"  {i}: (unreadable: {e})")
    for cfg in dev:
        print(f"  config {cfg.bConfigurationValue}: {cfg.bNumInterfaces} interface(s), "
              f"maxpower {cfg.bMaxPower * 2} mA")
        for intf in cfg:
            eps = " ".join(f"{e.bEndpointAddress:02x}/{ {1:'iso',2:'bulk',3:'intr'}[e.bmAttributes & 3] }"
                           f"/{e.wMaxPacketSize}" for e in intf)
            print(f"    intf {intf.bInterfaceNumber} alt {intf.bAlternateSetting} "
                  f"class {intf.bInterfaceClass:02x}: {eps}")


def fx2_ram_read(dev, addr, n):
    return bytes(dev.ctrl_transfer(0xC0, 0xA0, addr, 0, n, timeout=1000))


def test_bootrom(dev):
    print("\n[1] FX2 boot-ROM test (vendor request 0xA0, read 64 bytes of code RAM @0000):")
    try:
        d = fx2_ram_read(dev, 0x0000, 64)
        print("    OK ->", d.hex(" "))
        print("    => the device IS an FX2(LP) answering the Cypress boot loader protocol.")
        print("       It is waiting for firmware upload (C0-load mode).")
        # CPUCS register at E600: bit0 = 8051 reset
        try:
            cpucs = fx2_ram_read(dev, 0xE600, 1)
            print(f"    CPUCS(E600) = {cpucs.hex()}  (bit0=1 means 8051 held in reset)")
        except usb.core.USBError as e:
            print(f"    CPUCS read failed: {e}")
        return True
    except usb.core.USBError as e:
        print(f"    FAILED: {e}")
        print("    => either not an FX2 boot ROM, or firmware is already running.")
        return False


def try_other_vendor_requests(dev):
    print("\n[2] Other vendor IN requests (looking for firmware-level commands):")
    found = 0
    for req in range(0x00, 0x100):
        if req == 0xA0:
            continue
        try:
            d = bytes(dev.ctrl_transfer(0xC0, req, 0, 0, 64, timeout=200))
            print(f"    bRequest {req:02x}: {len(d)} bytes -> {d[:32].hex(' ')}")
            found += 1
        except usb.core.USBError:
            pass
    if not found:
        print("    none answered (expected for the bare boot ROM)")


def poke_endpoints(dev):
    print("\n[3] alt setting 1 + bulk IN reads (does anything stream without firmware?):")
    try:
        dev.set_configuration(1)
        usb.util.claim_interface(dev, 0)
        dev.set_interface_altsetting(0, 1)
    except usb.core.USBError as e:
        print(f"    could not select alt 1: {e}")
        return
    for ep in (0x81, 0x86, 0x88):
        try:
            d = dev.read(ep, 512, timeout=300)
            print(f"    ep{ep:02x}: got {len(d)} bytes: {bytes(d[:32]).hex(' ')}")
        except usb.core.USBError as e:
            print(f"    ep{ep:02x}: {e}")
    try:
        dev.set_interface_altsetting(0, 0)
    except usb.core.USBError:
        pass
    usb.util.release_interface(dev, 0)


def read_eeprom(dev, n):
    print(f"\n[4] boot EEPROM dump (first {n} bytes) via the fx2 package RAM bootloader:")
    try:
        import os
        import fx2
        from fx2 import FX2Device
        from fx2.format import input_data
    except ImportError:
        print("    pip install --user fx2")
        return
    usb.util.dispose_resources(dev)  # hand the device over to libusb1
    f = FX2Device(vendor_id=dev.idVendor, product_id=dev.idProduct)
    boot = os.path.join(os.path.dirname(fx2.__file__), "boot-cypress.ihex")
    with open(boot, "rb") as fh:
        f.load_ram(input_data(fh))  # RAM only; a power cycle restores the boot ROM
    time.sleep(0.5)
    data = None
    for width in (1, 2):  # small (24LC01/02) vs large (24LC64/128) EEPROM addressing
        try:
            d = bytes(f.read_boot_eeprom(0, n, width))
        except Exception as e:
            print(f"    addr_width={width}: {e}")
            continue
        print(f"    addr_width={width}: {d.hex(' ')}")
        if d and d[0] in (0xC0, 0xC2):
            data = d
            break
    if data is None:
        print("    no Cypress boot header found with either addressing mode")
        return
    if data[0] == 0xC0:
        vid = data[1] | (data[2] << 8)
        pid = data[3] | (data[4] << 8)
        did = data[5] | (data[6] << 8)
        print(f"    C0 header: VID={vid:04x} PID={pid:04x} DID={did:04x} config={data[7]:02x} "
              "=> 'C0 load': firmware comes from the host. Confirmed.")
    else:
        print("    C2 header: full firmware lives in the EEPROM (dump it all with fx2tool dump).")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scan", action="store_true", help="scan all vendor IN requests (slow-ish)")
    ap.add_argument("--poke", action="store_true", help="select alt 1 and try bulk reads")
    ap.add_argument("--eeprom", type=int, nargs="?", const=64, help="dump N bytes of boot EEPROM (loads a RAM bootloader)")
    args = ap.parse_args()
    devs = find_all()
    if not devs:
        sys.exit("no 2aa9:* device found. Is the camera plugged in?")
    for dev in devs:
        show(dev)
        try:
            dev.set_configuration()
        except usb.core.USBError as e:
            print(f"\ncannot open device: {e}\n-> install udev/99-ici-9320p.rules or run with sudo")
            continue
        boot = test_bootrom(dev)
        if args.scan:
            try_other_vendor_requests(dev)
        if args.poke:
            poke_endpoints(dev)
        if args.eeprom:
            read_eeprom(dev, args.eeprom)


if __name__ == "__main__":
    main()
