#!/bin/bash
# Grant haf_fyp_user read access to external HAF app schemas.
#
# Run as haf_admin AFTER HiveSense, btracker, and reptracker are installed.
# Safe to re-run — missing schemas are skipped with a NOTICE, not an error.
# Re-run once for each app that was installed after haf_fyp.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# This script's own role default, set BEFORE common.sh (which uses := and so only
# fills an unset value).
#
# common.sh defaults POSTGRES_USER to haf_fyp_owner, which is correct for every other
# script and wrong here. ALTER DEFAULT PRIVILEGES FOR ROLE <foreign owner> needs
# superuser or membership in that role, and haf_fyp_owner has neither -- but running
# this file as haf_fyp_owner does not FAIL. The seven schema-guarded blocks read
# "not found" (information_schema.schemata hides a schema the role has no rights on),
# the two unguarded hafd grants only WARN, and the self-heal loop NOTICEs once per
# owner. psql exits 0 and this script printed "Done." after healing nothing.
#
# Only docker-compose.yml's haf-fyp-setup service set haf_admin, and Dockerfile.sql
# sets no POSTGRES_USER, so every other documented invocation -- a host checkout,
# docker run on the SQL image, docker exec into a running container -- silently did
# nothing. The header above has always said haf_admin; now the code agrees with it.
: "${POSTGRES_USER:=haf_admin}"

source "${SCRIPT_DIR}/common.sh"

wait_for_postgres

# A deliberate override is still allowed, but say what it buys. Membership in the
# owner roles is enough, so this is a warning rather than a refusal.
if [ "$(psql_cmd -tAc "SELECT rolsuper FROM pg_roles WHERE rolname = current_user")" != "t" ]; then
    log "NOTE: connected as '${POSTGRES_USER}', which is not a superuser. Self-healing"
    log "      needs superuser or membership in each foreign app's owner role; without"
    log "      it, grants are applied one-shot and must be re-applied after upgrades."
fi

log "Granting cross-schema read access to haf_fyp_user..."
# Captured so the outcome can be acted on. psql exits 0 whether the pass healed
# everything or nothing, which is exactly how the wrong-role run used to pass silently.
#
# set +e around the capture is load-bearing. Under `set -e` a failing command
# substitution kills the script ON THE ASSIGNMENT, before the echo below can replay
# what psql wrote -- so a real SQL error (ON_ERROR_STOP=on) would leave the operator
# with a bare exit code and no diagnostics at all, in the one script whose whole
# purpose is to stop silent failures. Capture, restore -e, print, then decide.
set +e
grant_output="$(run_sql_file "${SCRIPT_DIR}/../db/cross_schema_grants.sql" 2>&1)"
grant_status=$?
set -e
echo "${grant_output}"

if [ "${grant_status}" -ne 0 ]; then
    log "ERROR: cross_schema_grants.sql failed (psql exit ${grant_status}) — see output above."
    exit "${grant_status}"
fi

if grep -q 'Self-healing FAILED' <<<"${grant_output}"; then
    log "ERROR: self-healing set nothing while targets are installed."
    log "       Grants are one-shot and will be lost the next time a foreign app is"
    log "       upgraded. Re-run as haf_admin, or as a member of the owner roles."
    exit 1
fi

if grep -q 'Self-healing incomplete' <<<"${grant_output}"; then
    log "WARNING: some targets could not be self-healed — see the line above."
fi

log "Done. Check NOTICE output above — re-run after installing any missing apps."
