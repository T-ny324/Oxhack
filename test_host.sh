#!/usr/bin/env bash
# Host-only checks (no Pico SDK, Wi-Fi or Wokwi): unit tests under ASan/UBSan.
set -euo pipefail
cd "$(dirname "$0")/host"
make test
echo "Optional: make demo | make demo-mqtt | make plan DAY=2 | python3 ../tools/check_against_lp.py"
