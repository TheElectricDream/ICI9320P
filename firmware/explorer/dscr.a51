; USB descriptors for the ICI9320P "explorer" firmware (derived from fx2lib's
; bulkloop example, LGPL). VID 2aa9 (same as the camera) PID 9f10 so the udev
; rule in udev/ matches. EP2 OUT bulk 512, EP6 IN bulk 512.

.module DEV_DSCR

DSCR_DEVICE_TYPE=1
DSCR_CONFIG_TYPE=2
DSCR_STRING_TYPE=3
DSCR_INTERFACE_TYPE=4
DSCR_ENDPOINT_TYPE=5
DSCR_DEVQUAL_TYPE=6
DSCR_INTERFACE_LEN=9
DSCR_ENDPOINT_LEN=7
ENDPOINT_TYPE_BULK=2

    .globl  _dev_dscr, _dev_qual_dscr, _highspd_dscr, _fullspd_dscr, _dev_strings, _dev_strings_end
    .area   DSCR_AREA   (CODE)

_dev_dscr:
    .db dev_dscr_end-_dev_dscr
    .db DSCR_DEVICE_TYPE
    .dw 0x0002          ; usb 2.0
    .db 0xff, 0xff, 0xff
    .db 64
    .dw 0xA92A          ; vendor id 0x2aa9 (byte swapped)
    .dw 0x109F          ; product id 0x9f10
    .dw 0x0100          ; bcdDevice 0.01
    .db 1, 2, 3         ; manufacturer, product, serial string idx
    .db 1
dev_dscr_end:

_dev_qual_dscr:
    .db dev_qualdscr_end-_dev_qual_dscr
    .db DSCR_DEVQUAL_TYPE
    .dw 0x0002
    .db 0xff, 0xff, 0xff
    .db 64
    .db 1
    .db 0
dev_qualdscr_end:

_highspd_dscr:
    .db highspd_dscr_end-_highspd_dscr
    .db DSCR_CONFIG_TYPE
    .db (highspd_dscr_realend-_highspd_dscr) % 256
    .db (highspd_dscr_realend-_highspd_dscr) / 256
    .db 1               ; n interfaces
    .db 1               ; config number
    .db 0
    .db 0x80            ; bus powered
    .db 0xFA            ; 500 mA
highspd_dscr_end:
    .db DSCR_INTERFACE_LEN
    .db DSCR_INTERFACE_TYPE
    .db 0, 0, 3
    .db 0xff, 0xff, 0xff
    .db 0
    .db DSCR_ENDPOINT_LEN
    .db DSCR_ENDPOINT_TYPE
    .db 0x82, ENDPOINT_TYPE_BULK, 0x00, 0x02, 0x00
    .db DSCR_ENDPOINT_LEN
    .db DSCR_ENDPOINT_TYPE
    .db 0x04, ENDPOINT_TYPE_BULK, 0x00, 0x02, 0x00
    .db DSCR_ENDPOINT_LEN
    .db DSCR_ENDPOINT_TYPE
    .db 0x86, ENDPOINT_TYPE_BULK, 0x00, 0x02, 0x00
highspd_dscr_realend:

    .even
_fullspd_dscr:
    .db fullspd_dscr_end-_fullspd_dscr
    .db DSCR_CONFIG_TYPE
    .db (fullspd_dscr_realend-_fullspd_dscr) % 256
    .db (fullspd_dscr_realend-_fullspd_dscr) / 256
    .db 1
    .db 1
    .db 0
    .db 0x80
    .db 0xFA
fullspd_dscr_end:
    .db DSCR_INTERFACE_LEN
    .db DSCR_INTERFACE_TYPE
    .db 0, 0, 3
    .db 0xff, 0xff, 0xff
    .db 0
    .db DSCR_ENDPOINT_LEN
    .db DSCR_ENDPOINT_TYPE
    .db 0x82, ENDPOINT_TYPE_BULK, 0x40, 0x00, 0x00
    .db DSCR_ENDPOINT_LEN
    .db DSCR_ENDPOINT_TYPE
    .db 0x04, ENDPOINT_TYPE_BULK, 0x40, 0x00, 0x00
    .db DSCR_ENDPOINT_LEN
    .db DSCR_ENDPOINT_TYPE
    .db 0x86, ENDPOINT_TYPE_BULK, 0x40, 0x00, 0x00
fullspd_dscr_realend:

.even
_dev_strings:
_string0:
    .db string0end-_string0
    .db DSCR_STRING_TYPE
    .db 0x09, 0x04
string0end:
_string1:
    .db string1end-_string1
    .db DSCR_STRING_TYPE
    .ascii 'I'
    .db 0
    .ascii 'C'
    .db 0
    .ascii 'I'
    .db 0
    .ascii '9'
    .db 0
    .ascii '3'
    .db 0
    .ascii '2'
    .db 0
    .ascii '0'
    .db 0
    .ascii 'P'
    .db 0
string1end:
_string2:
    .db string2end-_string2
    .db DSCR_STRING_TYPE
    .ascii 'e'
    .db 0
    .ascii 'x'
    .db 0
    .ascii 'p'
    .db 0
    .ascii 'l'
    .db 0
    .ascii 'o'
    .db 0
    .ascii 'r'
    .db 0
    .ascii 'e'
    .db 0
    .ascii 'r'
    .db 0
string2end:
_string3:
    .db string3end-_string3
    .db DSCR_STRING_TYPE
    .ascii '0'
    .db 0
    .ascii '0'
    .db 0
    .ascii '1'
    .db 0
string3end:
_dev_strings_end:
    .dw 0x0000
