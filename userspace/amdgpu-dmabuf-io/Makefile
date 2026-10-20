ROCM     := $(shell ls -d /opt/rocm/core-* 2>/dev/null | sort -V | tail -1)
CXX      ?= $(ROCM)/bin/hipcc
CXXFLAGS ?= -O3 -I/usr/include/libdrm -I$(ROCM)/include
LDFLAGS  ?= -luring -ldrm -ldrm_amdgpu -lhsa-runtime64 -L$(ROCM)/lib -Wl,-rpath,$(ROCM)/lib

all: test_p2p_io

test_p2p_io: test_p2p_io.cpp
	$(CXX) $(CXXFLAGS) $< $(LDFLAGS) -o $@

clean:
	rm -f test_p2p_io

.PHONY: all clean
