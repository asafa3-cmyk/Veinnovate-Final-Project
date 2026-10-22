#!/usr/bin/env bash
# run_edge_service.sh — start the production Jetson service against real BLE.
# The sibling runtime/model must already be installed. Production startup
# resolves and verifies it before BLE begins and requires --patient-id.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

python3 "${BUNDLE_ROOT}/receiver/veinnet_edge_service.py" "$@"
