#!/usr/bin/env bash
# setup.sh - build helper for C bridge on macOS/Linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="build"
DO_INSTALL=true

usage() {
  cat <<USAGE
Usage:
  ./setup.sh [--build|--test] [--no-install]

Options:
  --build        Build c/bridge (default)
  --test         Build then run c/test.sh (requires TEST_BOT_TOKEN)
  --no-install   Skip dependency install step
USAGE
}

for arg in "$@"; do
  case "$arg" in
    --build) MODE="build" ;;
    --test) MODE="test" ;;
    --no-install) DO_INSTALL=false ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown arg: $arg" >&2; usage; exit 1 ;;
  esac
done

cd "$SCRIPT_DIR"

if $DO_INSTALL; then
  if command -v brew >/dev/null 2>&1; then
    brew install libmicrohttpd cjson curl pkg-config
  elif command -v apt-get >/dev/null 2>&1; then
    echo "apt-get detected. Please install deps with:" >&2
    echo "  sudo apt-get install libmicrohttpd-dev libcjson-dev libcurl4-openssl-dev pkg-config" >&2
  else
    echo "No supported package manager found; install deps manually." >&2
  fi
fi

if [[ -n "${CFLAGS:-}" ]] || [[ -n "${LDFLAGS:-}" ]] || [[ -n "${LDLIBS:-}" ]]; then
  echo "Using existing CFLAGS/LDFLAGS/LDLIBS from environment."
else
  if command -v brew >/dev/null 2>&1; then
    export PKG_CONFIG_PATH="/opt/homebrew/opt/libmicrohttpd/lib/pkgconfig:/opt/homebrew/opt/libmicrohttpd/share/pkgconfig:/opt/homebrew/opt/cjson/lib/pkgconfig:/opt/homebrew/opt/cjson/share/pkgconfig:/opt/homebrew/opt/curl/lib/pkgconfig:/opt/homebrew/opt/curl/share/pkgconfig:${PKG_CONFIG_PATH:-}"
  fi
  if command -v pkg-config >/dev/null 2>&1; then
    if pkg-config --exists libmicrohttpd cjson; then
      export CFLAGS="$(pkg-config --cflags libmicrohttpd cjson) -O2 -Wall -Wextra -std=c11"
      export LDFLAGS="$(pkg-config --libs-only-L libmicrohttpd cjson)"
      export LDLIBS="$(pkg-config --libs-only-l libmicrohttpd cjson) -lcurl -lpthread"
    else
      echo "pkg-config could not find libmicrohttpd/cjson; falling back to Homebrew paths." >&2
      export CFLAGS="-O2 -Wall -Wextra -std=c11 -I/opt/homebrew/opt/libmicrohttpd/include -I/opt/homebrew/opt/cjson/include -I/opt/homebrew/opt/curl/include"
      export LDFLAGS="-L/opt/homebrew/opt/libmicrohttpd/lib -L/opt/homebrew/opt/cjson/lib -L/opt/homebrew/opt/curl/lib"
      export LDLIBS="-lmicrohttpd -lcjson -lcurl -lpthread"
    fi
  else
    if command -v brew >/dev/null 2>&1; then
      export CFLAGS="-O2 -Wall -Wextra -std=c11 -I/opt/homebrew/opt/libmicrohttpd/include -I/opt/homebrew/opt/cjson/include -I/opt/homebrew/opt/curl/include"
      export LDFLAGS="-L/opt/homebrew/opt/libmicrohttpd/lib -L/opt/homebrew/opt/cjson/lib -L/opt/homebrew/opt/curl/lib"
      export LDLIBS="-lmicrohttpd -lcjson -lcurl -lpthread"
    else
      echo "pkg-config not found; set CFLAGS/LDFLAGS/LDLIBS manually." >&2
    fi
  fi
fi

make clean
make

if [[ "$MODE" == "test" ]]; then
  if [[ -z "${TEST_BOT_TOKEN:-}" ]]; then
    echo "TEST_BOT_TOKEN not set" >&2
    exit 1
  fi
  ./test.sh
fi
