# Build the explorer firmware with sdcc + fx2lib.
# Uses the sdcc extracted under ~/.local/sdcc (no root) unless SDCC_HOME is set.
SDCC_HOME ?= $(HOME)/.local/sdcc/usr
export PATH := $(HOME)/.local/sdccbin:$(PATH)
export GCC_EXEC_PREFIX := $(SDCC_HOME)/libexec/sdcc/
export SDCCFLAGS := -I $(SDCC_HOME)/share/sdcc/include -L $(SDCC_HOME)/share/sdcc/lib/small
INCLUDES = $(SDCC_HOME)/share/sdcc/include
FX2LIBDIR = ../fx2lib/
BASENAME = explorer
SOURCES = explorer.c loader.c
A51_SOURCES = dscr.a51
VID = 0x2aa9
PID = 0x9f10
CODE_SIZE = --code-size 0x3800
XRAM_LOC = --xram-loc 0x3800
XRAM_SIZE = --xram-size 0x0200
INT2JT = -Wl"-b INT2JT=0x3f00" -Wl"-b LOADER=0x3a00"
include $(FX2LIBDIR)lib/fx2.mk

build/loader.rel: loader.c | build
	sdcc -mmcs51 $(SDCCFLAGS) --codeseg LOADER -c -o $@ -I $(FX2LIBDIR)include $<
