CC      ?= gcc
CFLAGS  ?= -O2 -Wall -Wextra
LDFLAGS ?= -lbpf -lmnl

all: knod_xdp_test

knod_xdp_test: knod_xdp_test.c
	$(CC) $(CFLAGS) $< $(LDFLAGS) -o $@

clean:
	rm -f knod_xdp_test

.PHONY: all clean
