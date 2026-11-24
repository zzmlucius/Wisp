CC ?= cc
AR ?= ar
CLANG ?= clang
BPFTOOL ?= bpftool
BUILD ?= bench/build
WITH_BPF ?= 0
CFLAGS ?= -O2 -g
CFLAGS += -std=c11 -Wall -Wextra -Wpedantic
LDLIBS += -lm
BPF_ARCH ?= $(shell uname -m | sed -e 's/x86_64/x86/' -e 's/aarch64/arm64/' -e 's/ppc64le/powerpc/' -e 's/riscv64/riscv/')
MODE := core
ifeq ($(WITH_BPF),1)
MODE := libbpf
CPPFLAGS += -DWITH_BPF $(shell pkg-config --cflags libbpf)
LDLIBS += $(shell pkg-config --libs libbpf)
endif
OBJDIR := $(BUILD)/$(MODE)
OBJECTS := $(addprefix $(OBJDIR)/,Monitor.o snap.o MonitorLoop.o Probe.o)
LIB := $(OBJDIR)/libwisp_monitor.a
CLI := $(OBJDIR)/wisp-collector

.PHONY: all bpf test
all: $(CLI) $(LIB)

$(OBJDIR)/%.o: %.c monitor.h internal.h Indicator.def probe_data.h
	mkdir -p $(OBJDIR)
	$(CC) $(CPPFLAGS) $(CFLAGS) -c $< -o $@

$(LIB): $(OBJECTS)
	$(AR) rcs $@ $^

$(CLI): $(OBJDIR)/collector.o $(LIB)
	$(CC) $(LDFLAGS) -o $@ $^ $(LDLIBS)

bpf:
	$(MAKE) BUILD="$(BUILD)" WITH_BPF=1 all $(BUILD)/scheduler.bpf.o

$(BUILD)/vmlinux.h: /sys/kernel/btf/vmlinux
	mkdir -p $(BUILD)
	$(BPFTOOL) btf dump file $< format c > $@.tmp
	mv $@.tmp $@

$(BUILD)/scheduler.bpf.o: scheduler.bpf.c probe_data.h $(BUILD)/vmlinux.h
	$(CLANG) -g -O2 -target bpf -D__TARGET_ARCH_$(BPF_ARCH) -I$(BUILD) -c $< -o $@

$(OBJDIR)/test_monitor: tests/test_monitor.c $(LIB)
	$(CC) $(CPPFLAGS) $(CFLAGS) -I. $(LDFLAGS) -o $@ $^ $(LDLIBS)

test: $(OBJDIR)/test_monitor
	$(OBJDIR)/test_monitor
