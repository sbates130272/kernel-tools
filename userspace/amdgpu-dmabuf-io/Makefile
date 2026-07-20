CXX      ?= hipcc
CXXFLAGS ?= -O3
LDFLAGS  ?= -luring -ldrm -lamdgpu

all: test_p2p_io

test_p2p_io: test_p2p_io.cpp
	$(CXX) $(CXXFLAGS) $< $(LDFLAGS) -o $@

clean:
	rm -f test_p2p_io

.PHONY: all clean
