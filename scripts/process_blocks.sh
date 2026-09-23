#!/bin/bash
# Starts the infinite block-processing loop.
# Calls haf_fyp_app.main() which blocks until stopped or killed.
#
# OPTIONS:                                                      # --- help-start ---
#   --host=VALUE   PostgreSQL host (default: $POSTGRES_HOST, else haf)
#   --port=NUMBER  PostgreSQL port (default: $POSTGRES_PORT, else 5432)
#   --user=VALUE   PostgreSQL user (default: $POSTGRES_USER, else haf_fyp_owner)
#   --postgres-url=URI  NOT SUPPORTED YET -- recognised and refused, with a
#                  message pointing at --host/--port/--user (--url= is the same)
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
        --host=*) require_flag_value "$arg"; POSTGRES_HOST="${arg#*=}" ;;
        --port=*) require_flag_value "$arg"; POSTGRES_PORT="${arg#*=}" ;;
        --user=*) require_flag_value "$arg"; POSTGRES_USER="${arg#*=}" ;;

        # RECOGNISED AND REFUSED, deliberately -- not warn-and-continue like --schema,
        # and not "is not a valid option" either.
        #
        # An earlier revision of this MR HONOURED it, handing the URI to psql as a
        # conninfo. That was worse than rejecting it, because only psql moved: every
        # message, the confirmation banner and the typed-the-database-name gate still
        # read ${POSTGRES_HOST}/${POSTGRES_DB}. So `uninstall_app --postgres-url=...`
        # dropped schemas on the URI's host while printing the discrete host, and
        # accepted the discrete database name as confirmation -- in the one gate whose
        # stated purpose is catching "the right command against the wrong host".
        #
        # Honouring it properly means the banner, the healthcheck's application_name
        # tag (the uninstall session guard excludes the probe by that literal), passing
        # the conninfo through to the healthcheck, a redactor that survives
        # ?password= and keyword/value forms, and the other scripts. That is tracked
        # separately.
        #
        # Refusing is safe here, unlike for --schema: --postgres-url appears in exactly
        # one upstream module file (hivemind.yaml, on hivemind-uninstall) and that file
        # carries no `restart:` key, so this cannot crash-loop a service. One clear
        # error beats a silent connection to the wrong database.
        --postgres-url=*|--url=*)
            log "ERROR: --postgres-url is not supported yet."
            log "       Use --host=, --port= and --user= instead. Accepting the URI"
            log "       would move only the psql connection: this script's messages,"
            log "       and the teardown's confirmation prompt, still read POSTGRES_HOST"
            log "       and POSTGRES_DB -- so it could act on one database while"
            log "       reporting another. Refusing is the safe answer until that is"
            log "       fixed throughout."
            exit 2
            ;;

        # balance_tracker's module file passes --schema= on ALL THREE verbs. Verified
        # against upstream rather than remembered: hive/balance_tracker's
        # scripts/process_blocks.sh has a `--schema=*) BTRACKER_SCHEMA=...` case, and
        # balance_tracker.yaml ships it on install_app and uninstall_app too. Not
        # "every module file" -- reputation_tracker.yaml and nft_tracker.yaml pass no
        # --schema at all, and haf_block_explorer.yaml passes it only on its two
        # balance-tracker services.
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

        # NO upstream module file passes --is_forking to a verb other than install_app
        # -- checked in balance_tracker.yaml, haf_block_explorer.yaml and
        # reputation_tracker.yaml, where it appears only on install services, and
        # balance_tracker's own process_blocks.sh/uninstall_app.sh have no case for it.
        # An earlier revision of this comment claimed the template passes it here; that
        # was wrong. Accepted anyway, for symmetry with install_app.sh and so a
        # hand-written module file that does pass it cannot crash-loop the service.
        # It could not be honoured in any case: db/haf_fyp_app.sql registers the context
        # non-forking. install_app.sh carries the long version of this note.
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

# The generic HAF block-processing driver (haf#341; /usr/local/bin in the psql
# base image) runs the registered haf_fyp_app.process_blocks per delivered
# range and idles on its own connection between blocks: the backend shows idle
# in pg_stat_activity, notifications are consumed, and no transaction spans a
# wait. hive.app_next_iteration also withholds blocks that hivemind has not
# committed - see db/register_app.sql.
#
# exec'd directly so it is PID 1 and receives SIGTERM itself; a bash parent
# swallows the signal until docker's kill (learned on reputation_tracker!223).
#
# The startup timestamp above stays OURS (database clock - see the healthcheck
# contract), so the driver's own container-clock marker is disabled with an
# empty --startup-marker.
#
# HAF_FYP_USE_LEGACY_LOOP=true forces the eternal CALL main() loop. The same
# fallback is taken automatically when the driver is not on PATH or when the
# HAF this node talks to has no application registry.
#
# The registry probe is NOT optional. The psql base image has shipped the driver
# since 14-3 (common-ci-configuration!386), so `command -v` alone would always
# succeed, and the driver's first act is `SELECT ... FROM hafd.applications`
# with no handler for a missing table: on a HAF that predates haf#341 (1.28.x)
# it exits non-zero before its first range, and because it is exec'd as PID 1
# under `restart: unless-stopped` that is an indefinite crash loop that the
# healthcheck's 72h start_period never surfaces. db/register_app.sql already
# warns and skips registration on such a HAF; this is the matching half.
registry_has_haf_fyp() {
    # Two statements, not one: PostgreSQL resolves every relation at plan time,
    # so a single query that names hafd.applications errors on a HAF without
    # it whatever to_regclass() says. Any psql failure counts as "not ready".
    [ "$(psql_cmd -Atq -c "SELECT to_regclass('hafd.applications') IS NOT NULL" 2>/dev/null || true)" = "t" ] || return 1
    [ "$(psql_cmd -Atq -c "SELECT EXISTS (SELECT 1 FROM hafd.applications WHERE name = 'haf_fyp')" 2>/dev/null || true)" = "t" ]
}

# "Not registered" has two causes that need OPPOSITE answers, and the probe above
# cannot tell them apart (#62):
#
#   no registry at all (1.28.x)  -> the legacy loop, which is what this app did
#                                   before haf#341 and is safe: nothing else is
#                                   driving the context.
#   the context belongs to       -> NOT the legacy loop. main() calls
#   ANOTHER application             hive.app_next_iteration on context haf_fyp,
#                                   and the owning application's driver is calling
#                                   it on the same context. That pointer has no
#                                   lock on the path taken when the stages need no
#                                   analysis, so two loops hand each other's ranges
#                                   out: blocks processed twice, blocks skipped,
#                                   for both applications. db/register_app.sql
#                                   leaves the install standing in this state
#                                   deliberately -- upstream added that quiet
#                                   return so an EMBEDDED app's install can be
#                                   re-run -- so the refusal has to live here.
#
# Park rather than exit: this runs as PID 1 under `restart: unless-stopped`, so a
# non-zero exit is an indefinite crash loop, and a crash loop is the one shape the
# healthcheck's 72h start_period cannot surface. Idle instead and let
# docker/block-processing-healthcheck.sh report it: no block is processed, so the
# container goes unhealthy and says why, once, in a log that is still readable.
context_taken_by_another_app() {
    [ "$(psql_cmd -Atq -c "SELECT to_regclass('hafd.applications') IS NOT NULL" 2>/dev/null || true)" = "t" ] || return 1
    [ "$(psql_cmd -Atq -c "SELECT EXISTS (SELECT 1 FROM hafd.applications WHERE name <> 'haf_fyp' AND contexts @> ARRAY['haf_fyp'])" 2>/dev/null || true)" = "t" ]
}

refuse_foreign_context() {
    owner=$(psql_cmd -Atq -c "SELECT name FROM hafd.applications WHERE name <> 'haf_fyp' AND contexts @> ARRAY['haf_fyp'] LIMIT 1" 2>/dev/null || true)
    log "ERROR: context haf_fyp belongs to application '${owner:-<unknown>}', which drives it"
    log "       itself. Running haf_fyp's loop as well would advance one context pointer from"
    log "       two places and lose blocks for both applications, so block processing will NOT"
    log "       start. Either install haf_fyp on a HAF where it owns its context, or let"
    log "       '${owner:-that application}' process it and stop this service."
    # Idle, so the healthcheck reports an unhealthy container rather than a restart loop.
    while true; do sleep 3600; done
}

if command -v haf_app_driver.py >/dev/null 2>&1 \
   && [ "${HAF_FYP_USE_LEGACY_LOOP:-false}" != "true" ]; then
    if registry_has_haf_fyp; then
        log "haf_fyp is registered in the HAF application registry; running haf_app_driver.py"
        exec haf_app_driver.py --app=haf_fyp \
            --postgres-url="postgresql://${POSTGRES_USER}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}?application_name=haf_fyp_block_processing" \
            --startup-marker=
    fi
    log "WARNING: haf_app_driver.py is on PATH but haf_fyp is not in the HAF application registry"
    log "         (HAF older than haf#341, or db/register_app.sql skipped it); falling back to"
    log "         the legacy CALL main() loop"
fi

# Checked on every path into the legacy loop, including HAF_FYP_USE_LEGACY_LOOP=true: the
# hazard is the loop itself, not how the loop was chosen.
if context_taken_by_another_app; then
    refuse_foreign_context
fi

log "haf_app_driver.py not used, falling back to the legacy CALL main() loop"
psql_cmd -c "CALL haf_fyp_app.main('haf_fyp');"

log "Block processing loop exited."
