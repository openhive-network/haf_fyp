#!/bin/bash
# Grant haf_fyp_user read access to external HAF app schemas.
#
# Run as haf_admin AFTER HiveSense, btracker, and reptracker are installed.
# Safe to re-run — missing schemas are skipped with a NOTICE, not an error.
# Re-run once for each app that was installed after haf_fyp.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

wait_for_postgres

log "Granting cross-schema read access to haf_fyp_user..."
run_sql_file "${SCRIPT_DIR}/../db/cross_schema_grants.sql"
log "Done. Check NOTICE output above — re-run after installing any missing apps."
