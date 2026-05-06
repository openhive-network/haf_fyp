#!/bin/bash
# Starts the infinite block-processing loop.
# Calls haf_fyp_app.main() which blocks until stopped or killed.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

wait_for_postgres

log "Starting block processing loop..."

psql_cmd -c "SELECT haf_fyp_app.main();"

log "Block processing loop exited."
