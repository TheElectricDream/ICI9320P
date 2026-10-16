# ICI 9320P – hardware findings

Date: 2026-10-16. Camera: ICI 9320P, SN 1526809, 7.5 mm f/1.4 lens, USB Mini-B.
Host used for analysis: x86_64 Fedora laptop, USB 2.0 port. Target: ARM Linux (Jetson).

## 1. What the camera looks like on the bus

```
Bus 001 Device 009: ID 2aa9:9f00
bcdUSB 2.00, High Speed (480 Mb/s), bus powered 100 mA
bDeviceClass/SubClass/Protocol = ff/ff/ff (vendor specific)
No manufacturer / product / serial string descriptors
bcdDevice 0.00
1 configuration, 1 interface, 4 alternate settings:
  alt 0: no endpoints
  alt 1: EP1 OUT/IN bulk 512, EP2 OUT bulk, EP4 OUT bulk, EP6 IN bulk, EP8 IN bulk
  alt 2: EP1 OUT/IN interrupt 64, EP2 OUT interrupt, EP4 OUT bulk, EP6 IN interrupt, EP8 IN bulk
  alt 3: EP1 OUT/IN interrupt 64, EP2 OUT isochronous, EP4 OUT bulk, EP6 IN isochronous, EP8 IN bulk
```

This descriptor set is, byte for byte, the **default descriptor table built into the
Cypress EZ-USB FX2LP (CY7C68013A) boot ROM** (EZ-USB TRM, "Default USB device"). The
only things that differ from a blank FX2LP (04b4:8613) are the VID/PID and bcdDevice,
which is exactly what a **"C0 load" boot EEPROM** supplies: 8 bytes holding
`C0, VID, PID, DID, config`. In that mode the chip enumerates with the vendor's IDs and
the Cypress loader protocol, and the **host driver uploads the real firmware into RAM**
over vendor request `0xA0` (write to `0xE600`/CPUCS to hold the 8051 in reset, write the
code, release reset). The firmware then usually disconnects and re-enumerates with its own
descriptors ("ReNumeration").

Evidence that supports this:

* vendor-specific class with zero string descriptors and DID 0 (a real firmware would
  normally carry strings / a nonzero device release);
* ICI's IR Flash manual has a "Camera Reset" menu item that "reboots the camera and
  re-establishes USB communication", and the installer runs a "Device Driver Installation
  Wizard" more than once, consistent with a Cypress CyUSB driver package whose INF
  points at a firmware script;
* the 1.1 W / 500 mA budget and a USB 2.0 Mini-B jack match an FX2LP bridging a
  parallel 14-bit sensor bus (FX2 GPIF / slave FIFO) to USB.

Consequence: **nothing camera-specific can be learned from the device until firmware
is loaded.** Without the vendor firmware the chip only answers the Cypress loader
requests.

## 2. Sensor / data format (from ICI spec sheet, rev 4.2021-001)

| item | value |
|---|---|
| array | 320 x 240 uncooled VOx microbolometer, 17 µm pitch |
| frame rate | 60 Hz (P series); S series is 9 Hz |
| dynamic range | 14-bit, "raw data" over USB |
| spectral band | 7–14 µm |
| NETD | < 20 mK |
| power | 5 V, < 1.1 W over USB |
| shutter | built in (flat-field correction) |

Expected raw frame: 320 x 240 x 16-bit = 153 600 bytes; at 60 Hz that is 9.2 MB/s,
comfortably inside USB 2.0 bulk bandwidth via the FX2 slave FIFO (EP6 IN or EP8 IN,
512-byte packets, quad-buffered in the FX2).

Radiometry (counts -> °C) is done on the host by IR Flash using factory calibration
files ("advanced calibration process at the factory and installed in this software
program"), so the camera most likely streams uncorrected or partly corrected counts and
the calibration tables live in the Windows install, not in the camera.

## 3. What is not available online

Checked on 2026-10-16: infraredcameras.com and buy.infraredcameras.com (now a
MultiSensorAI subsidiary; only 8000-series and FLIR Tau 2 cores are sold), the Wayback
Machine for infraredcamerasinc.com and infraredcameras.com (only PDF brochures; the
2016 `assets/9320.zip` holds datasheets, ICD drawings and the IR Flash v3 manual, no
drivers), GitHub code search for the VID/PID, usb.ids (vendor 0x2aa9 is unregistered),
Software Informer (IR Flash Medical 11.8.28 listing, no public installer). IR Flash is
delivered on a USB stick with the camera or by a link from ICI support.

## 4. Open questions the USB capture must answer

1. The FX2 firmware image (size, whether it is ReNumerated and to which PID).
2. Post-firmware descriptor set: which endpoints are used for video and for commands.
3. Init sequence: vendor control requests and/or EP OUT command packets sent by IR
   Flash before video starts (sensor power-up, FFC/shutter, gain mode, frame rate).
4. Framing of the video stream: frame header / sync word, line order, 14-in-16 bit
   packing, telemetry lines, whether frames are fixed 153 600 B or carry a header.
5. Whether the sensor core speaks a known serial protocol through the FX2 (e.g. FLIR Tau
   serial protocol or DRS Tamarisk), which would give command semantics for free.

## 5. Explorer firmware results (2026-10-16, firmware/explorer)

Boot EEPROM (I2C 0x50, 1-byte addressing) is a **C2 image containing only a 7-byte stub**:
`90 E6 80 E4 F0 80 FE` = `mov DPTR,#IFCONFIG; clr A; movx @DPTR,A; sjmp $`, then a final
record writing CPUCS=0. So the chip boots, sets IFCONFIG=0 (ports mode, external IFCLK,
IFCLK not driven) and idles while the USB core enumerates the default device with ICI's
IDs. The real firmware is still host-loaded via 0xA0. Our explorer firmware is loaded the
same way (tools/explorer.py does it automatically).

Pin survey with the explorer firmware (ports all inputs, 10 kHz sampling, 0.3 s):

| signal | observation | interpretation |
|---|---|---|
| PB0..PB7 | all toggling continuously | data bus FD[7:0]; the sensor side streams autonomously |
| PD2, PD4 | ~1.6 % duty pulses | line-rate sync pulses (or FD[10]/FD[12], to be confirmed) |
| PD0,1,3,5,6,7, PC, PE | static 0 | unused / not bonded (56-pin package has no PC/PE) |
| PA | 0x03 static | PA0,PA1 high; PA2 (SLOE) low; PA4/PA5 (FIFOADR) = 00; PA6 (PKTEND) low; PA7 (SLCS#) low |
| RDY0 | 36 transitions / 0.3 s = **60 Hz** | frame sync (or SLRD polled once per frame) |
| RDY1 | saturated | pixel-rate strobe: SLWR in slave-FIFO mode, or line valid |
| RDY2..5 | static | — |

I2C bus scan: 0x18, 0x30, 0x50, 0x51. 0x18 answers manufacturer ID 0x0054 (Microchip) and
device ID 0x2101 = **MCP98243** (temperature sensor + 256-byte EEPROM). Its EEPROM at
0x50 is the FX2 boot EEPROM; 0x30/0x51 are the chip's write-protect control addresses.
Temperature register 0x05 read 0xC22C = 34.75 °C. There is **no sensor-core I2C slave**,
so sensor commands must flow through the FIFO interface (an FPGA/CPLD on the sensor side)
or a UART.

Working hypothesis: the sensor-side logic is a slave-FIFO master writing 16-bit pixel data
into the FX2 at FIFOADR=00 (EP2) with SLWR on RDY1, SLCS# tied low, and frame sync on RDY0.

Slave-FIFO test (explorer firmware, EP2 IN AUTOIN, FIFOADR 00): **no data** in any
combination of 8/16-bit, active-high/low SLWR, sync/async, indexed/fixed flags. EP2 stays
empty, so the sensor side is not a slave-FIFO master. ICI's firmware must therefore run
the FX2 as GPIF master and sample the bus itself (or something must be enabled first).

Return-to-loader trick that worked without replugging (old firmware): write a small 8051
stub into free RAM at 0x3FC0 (EA=0; USBCS=DISCON, RENUM=0; ~0.5 s delay; USBCS=0; loop)
and patch the INT2 autovector entry at 0x3F00 to LJMP there; the next control request
triggers it and the core re-enumerates as the default device 2aa9:9f00. The current
firmware instead carries a resident 0xA0 loader (firmware/explorer/loader.c, linked at
0x3A00) so `tools/explorer.py reload` works any time.

## 6. Sensor interface decoded (2026-10-16)

Confirmed with GPIF captures (firmware/explorer, tools/explorer.py):

| FX2 pin | role |
|---|---|
| FD[7:0] = PB0..7, FD[15:8] = PD0..7 | 16-bit pixel data, little-endian 14-bit values (0x0000..0x3FFF) |
| RDY1 | pixel strobe, one pulse per pixel, about 4.6 MHz during active video |
| RDY0 | frame valid: high 16.4 ms, low 0.37 ms, period 16.78 ms = 59.6 Hz |
| IFCLK | **not driven** by the sensor side (FIFO registers can only be written with the internal IFCLK) |
| PA, I2C | no role in video; I2C has only an MCP98243 temperature sensor + boot EEPROM |

A frame is exactly 320 x 240 = 76800 words = 153600 bytes with no header or telemetry
line (single-frame capture gated on RDY0 returns exactly 153600 bytes). The sensor streams
continuously without any command. Capture method that works: FX2 as GPIF master, internal
48 MHz IFCLK, waveform: wait RDY0 low, wait RDY0 high, then per pixel: wait RDY1 high ->
sample on entry to a DP+DATA state -> hold (no re-execute) until RDY1 low; frame end when
RDY0 low. Important GPIF detail: in FIFO mode the waveform restarts at S0 after state 7
until the transaction count expires, so "single frame" must park in a looping state.

Raw values on a room scene sit around 5200-5400 counts with ~21 counts temporal noise per
pixel; radiometric calibration (counts -> °C) is still unknown (IR Flash held it).

## 7. Streaming result (2026-10-16, ici9320 package)

With hardware frame delimiting (GINT on the wait-for-next-frame state, INPKTEND in the
firmware main loop, ZEROLENIN on EP6) and an asynchronous libusb reader keeping 8-16
frame-sized transfers queued, `python3 -m ici9320 grab -n 600` delivers 600/600 complete
153600-byte frames at 59.9 fps with zero USB errors. Frame-to-frame mean absolute
difference is 3.3 counts and the median per-pixel temporal noise is 3.1 counts (the 16-21
counts measured earlier was misalignment, not sensor noise). Synchronous per-frame reads
lose whole packets whenever no transfer is pending (the FX2 FIFO holds only 2 KB = 220 us).

Open: radiometric calibration, shutter/FFC and sensor commands (candidates: PA0/PA1 GPIO,
EP4 OUT FIFO -> sensor logic, UART on the FX2's TXD0/RXD0 if the package has them), a C
library for the Jetson.

## 8. Shutter / flat-field correction (2026-10-16)

The camera performs its **own** flat-field correction: the user hears periodic clicks and the
stream shows the event. Recorded sequence (captures/ffc_event.bin, 60 Hz frames):
paddle closes over ~3 frames (mean rises from the scene level to ~5360 at 26.75 °C
housing, spatial std drops to ~11-13), then 1-4 **sentinel frames with every pixel equal**
(values seen: 392, 253), two more closed frames, then the paddle opens over ~3 frames and
the scene returns with a shifted offset (mean changed by +25..+80 counts, i.e. the NUC
table was updated). Whole event ~0.25 s. Cadence: about every 30 s while the housing is
warming, ~2 min once stable (12:38:11, 12:40:09; 14:53:05, 14:53:35, 14:54:07).

No manual trigger was found. Pulsing PA0-PA3, PA5-PA7 (20/50/500/2500 ms, both levels),
PC0-7, PE0-7 (20 ms high; 200 ms low re-tests), and all GPIF CTL0-5 level combinations
has no effect on the image; FLIR-Tau-style UART probes at 9600/57600/115200/921600 get
no reply. **PA4 driven low resets the whole camera** (FX2 re-enumerates as 2aa9:9f00), so it
is a reset or power-enable line and must never be driven. Conclusion: the sensor-side
logic owns the shutter; the SDK classifies and skips shutter frames (ICI9320.classify_frame)
and keeps the last closed-paddle frame + housing temperature as a radiometric reference.

Follow-up (14:55-15:02): PC2 and PE5 looked suspicious once each, but repeated 200 ms low
and high pulses every 6 s (10 pulses per pin) with sentinel-frame detection produced no
shutter event correlated with the pulses (one autonomous event fell 2.6 s after a PE5 high
pulse, in line with the ~2.5 min cadence at 31 °C). Sentinel values seen so far: 392 (at
36.5 °C and 31.75 °C) and 253 (26.75 °C), so the value is not simply temperature.
Untried on purpose: writing on the FD bus (GPIF write / EP4 OUT) while the sensor logic
drives it, because of bus-contention risk; PA4 (resets the camera).
