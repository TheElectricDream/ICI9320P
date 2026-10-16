# ICI9320P

An open-source, from-scratch driver / SDK for the ICI 9320P thermal camera (Infrared Cameras
Inc., 320x240 VOx microbolometer, 14-bit, 60 Hz, USB 2.0). No vendor software is needed.
Target platforms: x86-64 Linux for development, ARM Linux (NVIDIA Jetson) for deployment.
Everything is pure Python (python-libusb1, numpy) plus an 8051 firmware image, so it runs
unchanged on both.

## Status

* **Streaming works.** The camera is a Cypress EZ-USB FX2LP whose vendor firmware was never
  published; we wrote our own (`firmware/explorer`). It drives the FX2's GPIF engine to sample
  the sensor's 16-bit bus on each pixel strobe and delivers 320x240 raw 14-bit frames at the
  sensor's native 59.6 Hz, with hardware frame delimiting. See [docs/FINDINGS.md](docs/FINDINGS.md)
  for the full reverse-engineering record.
* **Open items:** radiometric calibration (counts to °C), shutter / flat-field control and any
  other sensor commands, and a C library for latency-critical use.

## Quick start

```bash
pip3 install --user libusb1 numpy pillow
sudo cp udev/99-ici-9320p.rules /etc/udev/rules.d/ && sudo udevadm control --reload-rules && sudo udevadm trigger
# plug the camera in (it enumerates as 2aa9:9f00; the SDK loads the firmware automatically)
python3 -m ici9320 info          # firmware version, housing temperature
python3 -m ici9320 grab -n 300   # stream 300 frames, report fps / completeness
python3 -m ici9320 png -o f.png --avg 16
```

```python
from ici9320 import ICI9320
with ICI9320() as cam:
    cam.start_stream()
    for frame in cam.frames(100):   # numpy uint16 (240, 320), 14-bit raw counts
        ...
    print(cam.temperature_c())      # housing temperature from the on-board MCP98243
```

## Layout

| path | purpose |
|---|---|
| `ici9320/` | the SDK: `device.py` (open, firmware load, async streaming), `protocol.py` (vendor requests, flags), `fx2loader.py`, packaged firmware in `firmware/ici9320.ihx` |
| `firmware/explorer/` | FX2 firmware source (sdcc + fx2lib): GPIF capture waveforms, I2C, UART bridge, pin probes, resident 0xA0 loader |
| `firmware/fx2lib/` | vendored fx2lib (LGPL) |
| `tools/explorer.py` | low-level exploration CLI (pins, activity, i2c, rdytrace, stream modes, frame, grab, reload) |
| `tools/probe_device.py` | identify device state, FX2 boot-ROM test, boot EEPROM dump |
| `tools/usbcap.py`, `tools/usbmon_capture.py` | USB capture analysis / recording (used before the firmware route succeeded) |
| `udev/99-ici-9320p.rules` | non-root device access |
| `docs/` | findings and the (now historical) capture guide |

## Building the firmware

Needs `sdcc`. On Fedora without root the RPM can be extracted under `~/.local/sdcc` (see
`firmware/explorer/Makefile`, which also sets `GCC_EXEC_PREFIX` for sdcc's preprocessor).

```bash
make -C firmware/explorer
python3 tools/explorer.py reload      # push a new build through the resident loader, no replug
cp firmware/explorer/build/explorer.ihx ici9320/firmware/ici9320.ihx
```
