#!/usr/bin/env bash
# build_firmware.sh — compile veinnet_sensor_v1 with arduino-cli.
# Run from anywhere; resolves paths relative to this script's location so
# the bundle is portable to any checkout path.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SKETCH_DIR="${BUNDLE_ROOT}/firmware/veinnet_sensor_v1"
FQBN="Seeeduino:nrf52:xiaonRF52840"

if ! command -v arduino-cli >/dev/null 2>&1; then
  echo "ERROR: arduino-cli not found on PATH. Install it first (see README.md)." >&2
  exit 1
fi

echo "Compiling ${SKETCH_DIR} for ${FQBN}..."
arduino-cli compile --fqbn "${FQBN}" "${SKETCH_DIR}"
