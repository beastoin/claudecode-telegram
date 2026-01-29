CC ?= cc
CFLAGS ?= -O2 -Wall -Wextra -std=c11
LDFLAGS ?=
LDLIBS ?= -lcurl -lmicrohttpd -lcjson -lpthread

BIN = bridge

all: $(BIN)

$(BIN): bridge.c
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ $< $(LDLIBS)

clean:
	rm -f $(BIN)

.PHONY: all clean
