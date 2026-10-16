"""Vendor-request numbers understood by the ICI9320P explorer/SDK firmware (firmware/explorer)."""
VID = 0x2AA9
PID_BARE = 0x9F00        # Cypress boot state after power-up (C2 stub in EEPROM, host loads firmware)
PID_FW = 0x9F10          # our firmware

VR_READ_PINS = 0x10
VR_ACTIVITY = 0x11
VR_I2C_SCAN = 0x12
VR_I2C_READ = 0x13
VR_I2C_WRITE = 0x14
VR_PORT_WRITE = 0x15
VR_OE_WRITE = 0x16
VR_RDY_TRACE = 0x17
VR_SET_CTL = 0x18
VR_UART_SETUP = 0x20
VR_UART_TX = 0x21
VR_UART_RX = 0x22
VR_XREG_READ = 0x30
VR_XREG_WRITE = 0x31
VR_STREAM_START = 0x40
VR_STREAM_STOP = 0x41
VR_STREAM_STATUS = 0x42
VR_COMMIT_TAIL = 0x43
VR_VERSION = 0x50
VR_FX2_RAM = 0xA0        # Cypress loader protocol, also handled by the resident loader

MODE_SLAVE = 0
MODE_GPIF_RDY = 1
MODE_GPIF_FREE = 2
MODE_GPIF_EDGE = 3
MODE_FRAME = 4           # one RDY0-aligned frame, then park
MODE_FRAMES = 5          # continuous RDY0-aligned frames, short/zero packet after each frame

# STREAM_START wIndex flag bits
F_WORDWIDE = 0x0001
F_INTCLK = 0x0002
F_48MHZ = 0x0004
F_CLKPOL = 0x0008
F_CLKOE = 0x0010
F_HIPINS = 0x0020
F_FIXEDFLAGS = 0x0040
F_ASYNC = 0x0080
F_GATEFULL = 0x0800
F_SAS = 0x1000
F_INV = 0x2000

WIDTH, HEIGHT = 320, 240
FRAME_WORDS = WIDTH * HEIGHT
FRAME_BYTES = FRAME_WORDS * 2     # 153600
EP_VIDEO = 0x86

# I2C devices on the camera
I2C_TEMP = 0x18          # MCP98243 temperature sensor (reg 0x05 = temperature, 0x06 manuf 0x0054, 0x07 dev 0x2101)
I2C_EEPROM = 0x50        # its 256-byte EEPROM = FX2 boot EEPROM (C2 stub)
