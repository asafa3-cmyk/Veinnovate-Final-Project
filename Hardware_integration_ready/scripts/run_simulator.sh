#!/usr/bin/env bash
# run_simulator.sh — run the edge orchestration against the deterministic
# simulated BLE source. Add --production to require and exercise the real
# sibling adapter/model without physical BLE hardware.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

python3 "${BUNDLE_ROOT}/receiver/veinnet_edge_service.py" --simulate "$@"
