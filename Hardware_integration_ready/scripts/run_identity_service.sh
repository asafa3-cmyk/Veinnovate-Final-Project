#!/usr/bin/env bash
# run_identity_service.sh — run the real edge service with the software-only
# POC adapter identity/assignment API mounted (AGENTS.md items 2-6), backed
# by SQLite stores under --identity-db-dir. Adds no BLE/model requirement of
# its own beyond whatever --simulate/--production already need — see
# veinnet_edge_service.py's --identity-db-dir flag.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DB_DIR="${BUNDLE_ROOT}/var/identity-db"

python3 "${BUNDLE_ROOT}/receiver/veinnet_edge_service.py" \
  --simulate --identity-db-dir "${DB_DIR}" "$@"
