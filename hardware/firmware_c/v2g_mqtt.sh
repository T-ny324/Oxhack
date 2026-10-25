#!/usr/bin/env bash
# V2G Pico 2 W / Wokwi MQTT test client (macOS/Linux)
# This script communicates with a SIMULATED converter only.
set -euo pipefail

BROKER="${MQTT_BROKER:-test.mosquitto.org}"
PORT="${MQTT_PORT:-1883}"
# Set MQTT_PREFIX to exactly match V2G_TOPIC_PREFIX in Pico CMake.
PREFIX="${MQTT_PREFIX:-v2g/oxhack-4000}"

usage() {
  cat <<'HELP'
V2G MQTT test client

Setup (macOS):
  brew install mosquitto
  export MQTT_PREFIX='v2g/oxhack-4000'  # default, matches Pico firmware
  # Optional: export MQTT_BROKER=your.broker.host MQTT_PORT=1883

Usage:
  ./v2g_mqtt.sh monitor               Subscribe to all project topics
  ./v2g_mqtt.sh charge [kW]           Send one positive setpoint (default 2.0)
  ./v2g_mqtt.sh discharge [kW]        Send one negative setpoint (default 2.0)
  ./v2g_mqtt.sh stop                  Send STOP
  ./v2g_mqtt.sh auto                  Resume automatic dummy plan
  ./v2g_mqtt.sh hold charge [kW]      Refresh setpoint every 2 seconds until Ctrl+C
  ./v2g_mqtt.sh hold discharge [kW]   Refresh setpoint every 2 seconds until Ctrl+C
  ./v2g_mqtt.sh help

Notes:
  - Positive power = charging; negative = discharging.
  - Use a unique topic prefix, not another team's prefix.
  - Plaintext/public broker: for Wokwi simulation only, NOT real hardware.
  - The firmware stops after ~5 seconds without refresh; send auto to resume the dummy plan.
HELP
}

require_tool() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing $1. Install with: brew install mosquitto" >&2
    exit 1
  }
}

validate_power() {
  # Enforce positive decimal magnitude <= demo 7.4 kW (no exponent/sign).
  local value="$1"
  if ! [[ "$value" =~ ^([0-9]+)(\.[0-9]+)?$ ]]; then
    echo "Invalid kW '$value': use a non-negative decimal number, e.g. 2.0" >&2
    exit 2
  fi
  awk -v p="$value" 'BEGIN { if (p <= 0 || p > 7.4) exit 1 }' || {
    echo "kW magnitude must be greater than 0 and at most 7.4 (demo limit)." >&2
    exit 2
  }
}

publish() {
  local message="$1"
  echo "[send] $PREFIX/setpoint -> $message"
  # -r is deliberately NOT used: commands must not be retained.
  mosquitto_pub -h "$BROKER" -p "$PORT" -q 1 \
    -t "$PREFIX/setpoint" -m "$message"
}

send_mode() {
  local mode="$1" magnitude="${2:-2.0}" sign
  validate_power "$magnitude"
  if [[ "$mode" == charge ]]; then sign='+'; else sign='-'; fi
  publish "SET_POWER ${sign}${magnitude}"
}

[[ $# -ge 1 ]] || { usage; exit 2; }
case "$1" in
  help|-h|--help) usage; exit 0 ;;
esac

if [[ -z "$PREFIX" || "$PREFIX" == *YOURID* ]]; then
  echo "Set MQTT_PREFIX to your actual Pico V2G_TOPIC_PREFIX first." >&2
  echo "Example: export MQTT_PREFIX='v2g/oxhack-team42'" >&2
  exit 2
fi

require_tool mosquitto_pub
case "$1" in
  monitor)
    require_tool mosquitto_sub
    echo "[monitor] $BROKER:$PORT  topic: $PREFIX/# (Ctrl+C to stop)"
    exec mosquitto_sub -h "$BROKER" -p "$PORT" -t "$PREFIX/#" -v
    ;;
  charge|discharge)
    send_mode "$1" "${2:-2.0}"
    ;;
  stop)
    publish "STOP"
    ;;
  auto)
    publish "AUTO"
    ;;
  hold)
    [[ $# -ge 2 && ( "$2" == charge || "$2" == discharge ) ]] || {
      echo "Usage: $0 hold charge|discharge [kW]" >&2; exit 2;
    }
    magnitude="${3:-2.0}"
    validate_power "$magnitude"
    echo "Refreshing every 2 seconds. Ctrl+C sends STOP."
    trap 'echo; echo "[hold] stopping"; publish "STOP" || true; exit 0' INT TERM
    while :; do
      send_mode "$2" "$magnitude"
      sleep 2
    done
    ;;
  *)
    usage; exit 2
    ;;
esac
