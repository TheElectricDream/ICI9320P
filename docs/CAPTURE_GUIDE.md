# Capturing the firmware and protocol from IR Flash

The camera will not do anything until the host uploads the vendor firmware, and the only
known source of that firmware and of the command protocol is ICI's IR Flash software
(Windows). One USB capture of IR Flash starting up and streaming for a few seconds gives
us everything: the firmware upload, the init sequence, and the video framing.

You need the IR Flash installer (the USB stick shipped with the camera, or a download
link from ICI support, `Install_IRFlashPro.exe` / `Install_IRFlash.exe`).

## Option A – a Windows PC with USBPcap (simplest)

1. Install IR Flash on the Windows machine and confirm it shows live video.
2. Install [Wireshark](https://www.wireshark.org/) with the **USBPcap** component.
3. Unplug the camera. In Wireshark start a capture on the `USBPcap` interface that
   corresponds to the root hub you will plug the camera into (enable "Capture from newly
   connected devices").
4. Plug the camera in, wait 5 s, start IR Flash, let it stream for ~10 s, then do a
   shutter / FFC, change frame rate or gain if the menu allows, exit IR Flash.
5. Stop the capture, save as `.pcapng`, copy it to `captures/` in this repo.

## Option B – Windows VM on the Linux laptop, capture with usbmon (no Windows PC needed)

QEMU/KVM is installed on the laptop. Run Windows 10 in QEMU with the camera passed
through, and capture on the Linux side so the full packets are seen:

```bash
sudo modprobe usbmon
sudo python3 tools/usbmon_capture.py --bus 1 -o captures/irflash.pcap   # bus from lsusb
# then start the VM with the camera attached:
qemu-system-x86_64 -enable-kvm -m 4G -cpu host -smp 4 \
    -drive file=win10.qcow2,format=qcow2 \
    -usb -device usb-ehci,id=ehci \
    -device usb-host,bus=ehci.0,vendorid=0x2aa9 \
    -device usb-host,bus=ehci.0,vendorid=0x2aa9,productid=0x9f01   # add the post-firmware PID once known
```

Important: after the firmware upload the camera re-enumerates with a new PID; pass the
device through by `vendorid` only (as above) so QEMU re-attaches it to the VM. Install
IR Flash inside the VM, stream for ~10 s, exit. Stop the capture with Ctrl-C.

(VirtualBox also works: add a USB filter on vendor 2AA9 and capture with usbmon the same way.)

## Analysing the capture

```bash
python3 tools/usbcap.py captures/irflash.pcap                    # overview
python3 tools/usbcap.py captures/irflash.pcap --fw-out firmware   # writes firmware/fx2_image0.ihex
python3 tools/usbcap.py captures/irflash.pcap --ctrl              # every control request
python3 tools/usbcap.py captures/irflash.pcap --timeline          # init sequence
python3 tools/usbcap.py captures/irflash.pcap --bulk 0x86 --dump 4
```

Then on Linux:

```bash
python3 tools/probe_device.py            # confirm bare boot-ROM state
python3 -c "from ici9320 import *; import usb; d=find_bare_device(); \
    load_firmware(d, parse_ihex(open('firmware/fx2_image0.ihex').read())); print(wait_for_device())"
lsusb -v -d 2aa9:                        # new descriptors after ReNumeration
```

## Alternative if IR Flash cannot be run at all

If the installer is really lost, the only remaining path is the ICI Linux SDK (they
advertise x86/x64/ARM builds; contact support with the serial number), or extracting the
firmware from the installed Windows files (`*.hex`, `*.bix`, `*.iic`, `*.spt` under
`C:\Program Files*\ICI*` or embedded as a resource in the driver `.sys`/`.dll`). The
`tools/usbcap.py --fw-out` output format (Intel HEX) is what `ici9320.fx2loader` expects.
