#!/usr/bin/env bash
# flash_firmware.sh <port> — compile + upload veinnet_sensor_v1.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SKETCH_DIR="${BUNDLE_ROOT}/firmware/veinnet_sensor_v1"
FQBN="Seeeduino:nrf52:xiaonRF52840"
PORT="${1:-}"

if [[ -z "${PORT}" ]]; then
  echo "Usage: $0 <serial-port>  (e.g. $0 /dev/tty.usbmodem1101)" >&2
  echo "Run 'arduino-cli board list' to find the port." >&2
  exit 1
fi

"${SCRIPT_DIR}/build_firmware.sh"

echo "Uploading to ${PORT}..."
arduino-cli upload --fqbn "${FQBN}" --port "${PORT}" "${SKETCH_DIR}"
