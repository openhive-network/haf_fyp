#!/bin/bash
# Starts the infinite block-processing loop.
# Calls haf_fyp_app.main() which blocks until stopped or killed.
#
# OPTIONS:                                                      # --- help-start ---
#   --host=VALUE   PostgreSQL host (default: $POSTGRES_HOST, else haf)
#   --port=NUMBER  PostgreSQL port (default: $POSTGRES_PORT, else 5432)
#   --user=VALUE   PostgreSQL user (default: $POSTGRES_USER, else haf_fyp_owner)
#   --schema=NAME  Accepted for haf_api_node module-file compatibility; ignored
#                  with a warning unless it names haf_fyp_app
#   --is_forking=  Accepted likewise; this app's context is always non-forking
#   --help,-h,-?   Show this and exit                           # --- help-end ---

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

print_help() {
    # See scripts/install_app.sh for why the markers carry a character class.
    sed -n '/--- help-sta[r]t ---/,/--- help-en[d] ---/p' "$0" | sed 's/^# //; s/^#$//; s/ *# --- help-[a-z]* ---//'
}

# This loop exists because docker/docker_entrypoint.sh forwards --host here, as
# balance_tracker's entrypoint does. Without it the flag would be accepted by the
# entrypoint and then silently dropped by this script -- the same "looks like it
# worked while doing nothing" failure that install_app.sh's fallthrough was added
# to remove, and which a module file copied from an upstream app would hit.
for arg in "$@"; do
    case "$arg" in
        --host=*) POSTGRES_HOST="${arg#*=}" ;;
        --port=*) POSTGRES_PORT="${arg#*=}" ;;
        --user=*) POSTGRES_USER="${arg#*=}" ;;

        # haf_api_node's module template passes --schema= on EVERY verb. Verified
        # against upstream rather than remembered: hive/balance_tracker's
        # scripts/process_blocks.sh has a `--schema=*) BTRACKER_SCHEMA=...` case, and
        # balance_tracker.yaml ships it on install_app and uninstall_app too.
        #
        # !50 gave all three of this app's option loops an `exit 2` fallthrough and no
        # --schema case, so a haf_fyp module file copied from that template -- the whole
        # point of #27 -- exited 2 on every verb. On install that is a crash-loop under
        # haf-fyp-setup's `restart: on-failure` with every other service waiting on
        # service_completed_successfully, i.e. the stack never starts. It is also a
        # REGRESSION: before !50 install_app's loop had no fallthrough, so the flag was
        # ignored and the install worked.
        #
        # WARN and continue, for the same reason --is_forking below does: the flag
        # cannot change behaviour here (the schema names are fixed in the SQL), so
        # refusing buys nothing and costs the deployment.
        --schema=*)
            if [ "${arg#*=}" != "haf_fyp_app" ]; then
                log "WARNING: --schema=${arg#*=} is ignored."
                log "         haf_fyp owns two fixed schemas, haf_fyp_app and"
                log "         haf_fyp_endpoints, named in the SQL. Continuing."
            fi
            ;;

        # haf_api_node's module template defaults --is_forking from a node-wide
        # ${IS_FORKING:-TRUE}, so it arrives here without anyone choosing it for this
        # app. It cannot be honoured -- db/haf_fyp_app.sql registers the context
        # non-forking -- and it cannot be refused either, for the reason above.
        # install_app.sh carries the long version of this note.
        --is_forking=*)
            case "${arg#*=}" in
                FALSE|false|False)
                    log "--is_forking=FALSE matches this app's context; nothing to do."
                    ;;
                *)
                    log "WARNING: --is_forking=${arg#*=} is ignored; haf_fyp's HAF context"
                    log "         is registered NON-forking by design. Continuing."
                    ;;
            esac
            ;;

        --help|-h|-\?) print_help; exit 0 ;;
        *) log "ERROR: '${arg}' is not a valid option"; print_help; exit 2 ;;
    esac
done

wait_for_postgres

# When block processing started, for docker/block-processing-healthcheck.sh (#27).
# That check reports healthy only if hafd.contexts.last_active_at is BOTH recent and
# later than this timestamp; without the second half, a container that has just
# restarted is judged by a heartbeat the previous process wrote.
#
# Taken from the DATABASE clock, not from `date`. Two reasons, and the first one is
# not a preference:
#
#   1. hafd.contexts.last_active_at is `timestamp WITHOUT time zone`, written by
#      hive.app_next_iteration as `__now TIMESTAMP := NOW()` -- i.e. rendered in the
#      SERVER's session time zone, whatever that is. A timestamp produced here, in
#      the container, is only comparable to it if the container and the server agree
#      on the zone. They usually do, and upstream relies on that; when they do not
#      the comparison is wrong in the FAIL-OPEN direction -- a server ahead of the
#      container makes `now() - last_active_at` negative, so the freshness test
#      passes forever and a dead loop reports healthy. Reading the clock from the
#      same server that writes last_active_at removes the assumption entirely.
#   2. `date --utc --iso-8601=seconds` is a GNU long option. The image is Alpine
#      (Dockerfile.sql's base adds packages with apk), so `date` is BusyBox, which
#      has no --iso-8601 -- it would have exited non-zero here, under `set -e`,
#      before the loop ever started. balance_tracker, haf_block_explorer and
#      hivesense all write `date -uIseconds` for exactly this reason.
#
# Runs after wait_for_postgres because it now needs a connection. That also makes
# the file's absence mean "not processing yet" more precisely than before.
# Written via a temp file and mv, not `psql > file`. A redirect truncates BEFORE psql
# runs, so a psql that fails after pg_isready succeeded (auth change, dropped
# connection) leaves a zero-byte file -- which passes an existence check and then makes
# the reader cast '' to a timestamp and report a syntax error instead of "not started".
# mv within the same directory is atomic, so the reader sees either the old content or
# a complete new value, never an empty one.
STARTUP_TIME_FILE="${HAF_FYP_STARTUP_TIME_FILE:-/tmp/block_processing_startup_time.txt}"
_startup_tmp="${STARTUP_TIME_FILE}.tmp.$$"
psql_cmd -Atq -c "SELECT now()::timestamp" > "${_startup_tmp}"
mv "${_startup_tmp}" "${STARTUP_TIME_FILE}"
log "Block processing start time recorded: $(cat "${STARTUP_TIME_FILE}")"

log "Starting block processing loop..."

psql_cmd -c "CALL haf_fyp_app.main('haf_fyp');"

log "Block processing loop exited."
