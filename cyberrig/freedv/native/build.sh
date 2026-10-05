#!/bin/bash
# Build librade_speech.so against a rade_c checkout (RADE_SRC). Run from anywhere.
set -euo pipefail
RADE_SRC="${RADE_SRC:-$HOME/src/rade_c}"
OPUS="${RADE_SRC}/build/build_opus-prefix/src/build_opus"
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${OUT:-$HOME/opt/freedv-rade/lib}"
mkdir -p "$OUT"

cc -shared -fPIC -O2 -o "$OUT/librade_speech.so" \
  "$HERE/rade_speech.c" \
  -I"$RADE_SRC/src" \
  -I"$OPUS/include" \
  -I"$OPUS/dnn" \
  -I"$OPUS/celt" \
  -I"$OPUS/silk" \
  -L"$RADE_SRC/build/src" -Wl,-rpath,"$RADE_SRC/build/src" -lrade \
  -Wl,--whole-archive "$OPUS/.libs/libopus.a" -Wl,--no-whole-archive \
  -lm -lpthread
echo "built $OUT/librade_speech.so"
ls -l "$OUT/librade_speech.so"
