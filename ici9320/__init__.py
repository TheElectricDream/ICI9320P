"""
ici9320 - reverse-engineered host library for the ICI 9320P thermal camera.

The camera is a Cypress FX2LP that needs host-loaded firmware; this package ships its own
(ici9320/firmware/ici9320.ihx, built from firmware/explorer) and streams 320x240 14-bit raw
frames at 60 Hz. Pure python: python-libusb1 + numpy, runs on x86 and ARM (Jetson).

    from ici9320 import ICI9320
    with ICI9320() as cam:
        cam.start_stream()
        for frame in cam.frames(10):      # numpy uint16 (240, 320)
            ...
"""
from .device import ICI9320  # noqa: F401
from .fx2loader import parse_ihex, load_firmware, find_bare_device, wait_for_device  # noqa: F401
from . import protocol  # noqa: F401

__version__ = "0.1.0"
