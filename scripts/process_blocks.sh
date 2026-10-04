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

# "Not processing, and why" for docker/block-processing-healthcheck.sh (#76). The check
# reports unhealthy with this reason before it looks at any heartbeat -- which alone cannot
# tell: hafd.contexts.last_active_at is per CONTEXT, and while another application drives
# haf_fyp its loop keeps that fresh, so a parked container passed the check it was parked
# for.
#
# It starts out saying "starting", and is removed only on the two paths that go on to
# process: right before the driver is exec'd, and once the ownership check has answered
# "nobody". Every other path keeps a reason -- parked because another application owns the
# context, waiting on a probe that cannot answer, or a probe that simply hangs -- so there
# is no window in which the heartbeat speaks for a container that is not processing.
# Written rather than merely cleared at start for a second reason: `docker restart` keeps
# the container's /tmp, and a reason from the previous run must not outlive it.
PARKED_FILE="${HAF_FYP_PARKED_FILE:-/tmp/block_processing_parked.txt}"

# Atomic, like the startup file below: the reader sees the old reason or the new one.
# Best-effort: a reason that cannot be recorded must not end the script. Under set -e that
# is an exit, and under a restart policy a crash loop -- what parking exists to avoid.
# The cost of a failed write is the old behaviour: the check falls back to the heartbeat,
# which the owning application keeps fresh, and only the WARNING says so.
mark_parked() {
    local tmp="${PARKED_FILE}.tmp.$$"
    if ! { printf '%s\n' "$*" > "${tmp}" && mv "${tmp}" "${PARKED_FILE}"; } 2>/dev/null; then
        rm -f "${tmp}" 2>/dev/null || true
        log "WARNING: could not record this for the healthcheck in ${PARKED_FILE}: $*"
    fi
}
mark_parked "starting: block processing has not begun (ownership not yet checked)"

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
    # it whatever to_regclass() says. Any psql failure counts as "not ready" --
    # which is safe HERE, because it only chooses the legacy loop, and every path
    # into that loop passes the ownership check below, which fails closed (#74).
    # The failure is logged rather than discarded, so a node silently on the
    # fallback says why.
    local answer err
    err=$(mktemp)
    answer=$(psql_cmd -Atq -c "SELECT to_regclass('hafd.applications') IS NOT NULL" 2>"$err") \
        && [ "$answer" = "t" ] \
        && answer=$(psql_cmd -Atq -c "SELECT EXISTS (SELECT 1 FROM hafd.applications WHERE name = 'haf_fyp')" 2>"$err") \
        && [ "$answer" = "t" ] && { rm -f "$err"; return 0; }
    if [ -s "$err" ]; then
        log "WARNING: the application-registry probe failed: $(tr '\n' ' ' < "$err")"
    fi
    rm -f "$err"
    return 1
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
# Park rather than exit: this runs as PID 1 under a restart policy (`unless-stopped`
# here, `on-failure` in haf_api_node's module), so a non-zero exit is an indefinite crash
# loop. Idle instead, and say why once, in a log that is still readable -- and in PARKED_FILE, which
# docker/block-processing-healthcheck.sh turns into a failure (#76). Docker counts no
# failure inside the 72h start_period until a check first succeeds, which a parked one
# never does, so there the container shows `health: starting`, with the reason in its
# health log (`docker inspect`), and `unhealthy` after it.
#
# FAILS CLOSED (#74). An earlier version read a failed query -- a dropped connection, a
# statement timeout, a permissions error -- as "nobody else owns it", discarded the error,
# and went on to CALL main() on a context another application's driver may be advancing.
# This refusal exists to prevent exactly that, so a probe that cannot answer must not be
# taken as a "no".
#
# One statement, through haf_fyp_app.context_claimed_by_other_app(): the same question
# db/haf_fyp_app.sql's pointer repair, db/register_app.sql and install_app.sh step 14 ask
# (#65), with the registry probe already inside it (NULL on a HAF without one), and an
# ORDER BY, so the owner it names does not vary from run to run. install_app.sh loads that
# function at step 2, and both deployments start this service only after the install has
# completed (service_completed_successfully on haf-fyp-setup here, on haf-fyp-install in
# haf_api_node's module). A container restarted on its own after an image upgrade can
# still meet a database installed before the function existed; that is reported below.
#
# Prints the owning application's name, or nothing. Returns non-zero, with psql's error on
# stderr, when it could not find out.
context_claimed_by() {
    psql_cmd -Atq -c "SELECT COALESCE(haf_fyp_app.context_claimed_by_other_app('haf_fyp'), '')"
}

# The first wait between attempts, in seconds. Anything but a whole number from 1 to 99999
# falls back to 10: a value `sleep` rejects would exit this script under `set -e` (a crash
# loop), and 0 would query the database in a tight loop. Read as DECIMAL (10#): bash
# arithmetic takes a leading 0 as octal, and `08` then raised "value too great for base"
# inside the function below -- an expansion error, which set -e does not stop and which
# abandons the whole function, skipping the ownership check it exists to make.
ownership_retry_seconds() {
    local v="${HAF_FYP_OWNERSHIP_RETRY_SECONDS:-10}"
    case "$v" in
        '' | *[!0-9]* | ??????*) v=10 ;;
    esac
    v=$((10#$v))
    [ "$v" -ge 1 ] || v=10
    echo "$v"
}

# Waits until the ownership question has an answer, then parks if another application
# owns the context. Retries rather than exiting (an exit is a crash loop), backing off
# from the interval above to five minutes, and prints the full error only when it
# changes: a probe that fails for days must not bury its first, useful message under
# thousands of copies in a log nothing rotates.
refuse_if_another_application_owns_the_context() {
    # `last` starts at a value no error text can equal, so the first failure prints in
    # full even when psql wrote nothing to stderr.
    local owner err msg last=$'\x01' attempt=0 wait_s cap
    wait_s=$(ownership_retry_seconds)
    cap=$(( wait_s > 300 ? wait_s : 300 ))
    err=$(mktemp)
    until owner=$(context_claimed_by 2>"$err"); do
        attempt=$((attempt + 1))
        msg=$(tr '\n' ' ' < "$err")
        if [ "$msg" != "$last" ]; then
            log "ERROR: could not find out whether another application owns context haf_fyp:"
            log "       ${msg}"
            case "$msg" in
                *"context_claimed_by_other_app"*"does not exist"* | *'schema "haf_fyp_app" does not exist'*)
                    log "       haf_fyp_app.context_claimed_by_other_app() is missing, so this database was"
                    log "       installed by an older haf_fyp than this image. Re-run the install with this"
                    log "       image: \`up --build haf-fyp-setup\` here (README, step 2), or the"
                    log "       haf-fyp-install service in haf_api_node's module."
                    ;;
            esac
            log "       Block processing will NOT start until that is known; retrying in ${wait_s}s."
            mark_parked "waiting: cannot find out whether another application owns context haf_fyp: ${msg}"
            last="$msg"
        else
            log "ERROR: still cannot find out who owns context haf_fyp (attempt ${attempt}, same error); retrying in ${wait_s}s."
        fi
        sleep "$wait_s"
        wait_s=$(( wait_s * 2 > cap ? cap : wait_s * 2 ))
    done
    rm -f "$err"
    if [ -z "$owner" ]; then
        ownership_checked=1
        return 0
    fi

    log "ERROR: context haf_fyp belongs to application '${owner}', which drives it"
    log "       itself. Running haf_fyp's loop as well would advance one context pointer from"
    log "       two places and lose blocks for both applications, so block processing will NOT"
    log "       start. Either install haf_fyp on a HAF where it owns its context, or let"
    log "       '${owner}' process it and stop this service."
    log "       Removing haf_fyp from such a node needs the context detached first; README,"
    log "       \"If another application owns the context\", has the SQL (#66)."
    mark_parked "parked: context haf_fyp belongs to application '${owner}'; haf_fyp's block processing will not start"
    # Idle rather than exit: a restart loop. PARKED_FILE is what reports it (#76).
    while true; do sleep 3600; done
}

# The --lock flags for haf_app_driver.py: haf_fyp_app.block_processor_lock_names(), the
# list the legacy loop takes, so the two paths cannot disagree. It leaves 'hivemind' out
# while hivemind is not installed (#79) -- see that function for why.
#
# If the question cannot be asked, BOTH names are passed: that is the list before #79, and
# a processor holding one lock too many is safer than one reading hivemind_app unlocked.
# Sets the array `lock_flags`; an answer that is not plain names is treated as a failure.
block_processor_lock_flags() {
    local names name err
    lock_flags=()
    err=$(mktemp)
    if names=$(psql_cmd -Atq -c "SELECT array_to_string(haf_fyp_app.block_processor_lock_names(), ' ')" 2>"$err") \
       && [[ "$names" =~ ^[a-z_]+(\ [a-z_]+)*$ ]]; then
        :
    else
        log "WARNING: could not ask which application locks to hold ($(tr '\n' ' ' < "$err")); holding haf_fyp and hivemind"
        names="haf_fyp hivemind"
    fi
    rm -f "$err"
    for name in $names; do
        lock_flags+=("--lock=${name}")
    done
    case " $names " in
        *" hivemind "*) ;;
        *) log "hivemind is not installed (no hivemind_app.hive_posts): its lock is not taken, so its installer can run. Once it is installed: stop block processing, re-run the haf_fyp install, start it again." ;;
    esac
}

if command -v haf_app_driver.py >/dev/null 2>&1 \
   && [ "${HAF_FYP_USE_LEGACY_LOOP:-false}" != "true" ]; then
    if registry_has_haf_fyp; then
        log "haf_fyp is registered in the HAF application registry; running haf_app_driver.py"
        # --lock: HAF's shared block-processor locks (#67), held on the driver's own
        # connection and re-taken on every reconnect, so install_app.sh cannot run over a
        # live processor. The same names as haf_fyp_app.take_block_processor_locks(),
        # which the legacy loop below takes -- block_processor_lock_flags asks the database
        # for them. This path runs only on a HAF with the registry, and every such HAF has
        # the lock functions.
        #
        # The "starting" reason goes now: from here the driver's heartbeat is the signal
        # (#76). A registered haf_fyp owns its context, so there is nothing to refuse.
        block_processor_lock_flags
        rm -f "${PARKED_FILE}"
        exec haf_app_driver.py --app=haf_fyp \
            --postgres-url="postgresql://${POSTGRES_USER}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}?application_name=haf_fyp_block_processing" \
            "${lock_flags[@]}" \
            --startup-marker=
    fi
    log "WARNING: haf_app_driver.py is on PATH but haf_fyp is not in the HAF application registry"
    log "         (HAF older than haf#341, db/register_app.sql skipped it, or the probe failed --"
    log "         see above); falling back to the legacy CALL main() loop until the next restart"
fi

# Checked on every path into the legacy loop, including HAF_FYP_USE_LEGACY_LOOP=true: the
# hazard is the loop itself, not how the loop was chosen.
#
# And checked that it FINISHED. bash abandons a function on an expansion error without
# stopping the script, even under `set -e` -- that is how an interval of `08` once skipped
# this check entirely -- so the go-ahead is a flag set on the one path that answered
# "nobody", not the mere fact that the call returned.
ownership_checked=
refuse_if_another_application_owns_the_context
if [ "$ownership_checked" != 1 ]; then
    log "ERROR: the ownership check did not complete, so block processing will NOT start."
    log "       This is a bug in scripts/process_blocks.sh; the lines above say where it stopped."
    mark_parked "parked: the ownership check did not complete (a bug in process_blocks.sh; see the log)"
    while true; do sleep 3600; done
fi
# Answered "nobody": the loop may start, and the heartbeat is the signal from here (#76).
rm -f "${PARKED_FILE}"

log "haf_app_driver.py not used, falling back to the legacy CALL main() loop"

# CALL main() is ONE statement that runs for as long as the processor does, and its session
# holds HAF's block-processor locks (#67). Stopping the container used to end psql but not
# that session: the backend notices a vanished client only when a write to it fails, and
# testapi's logs show old main() backends outliving their container until the new one
# started. An install started in that window finds the lock held and skips, exit 0.
#
# So the stop is passed on, to the SERVER: on TERM or INT this script cancels the statement
# with pg_cancel_backend, finding the session by an application_name no other session has.
# main() is cancelled -- its current range rolls back and is redone on the next start, as
# after any crash -- psql exits, the session closes, and the lock goes with it. (Not
# SIGINT to psql: a non-interactive shell starts background jobs with SIGINT ignored.)
# If the server cannot be reached to cancel, psql is killed instead. psql runs in the
# background so that this script, PID 1, is free to take the signal.
#
# client_connection_check_interval, set in the same session, is the backstop for a hard
# kill (SIGKILL, OOM) where no handler runs: the server then checks the client every 5s
# while main() runs, instead of never. PostgreSQL 14 and later; HAF runs 17.
main_app_name="haf_fyp_legacy_main_${HOSTNAME:-$$}"
PGAPPNAME="${main_app_name}" psql_cmd -c "SET client_connection_check_interval = '5s'" \
         -c "CALL haf_fyp_app.main('haf_fyp');" &
main_pid=$!
stop_main() {
    log "Stopping: cancelling CALL main() so its session, and its HAF lock, end now."
    psql_cmd -Atq -c "SELECT pg_cancel_backend(pid) FROM pg_stat_activity
                      WHERE application_name = '${main_app_name}' AND pid <> pg_backend_pid()" \
        >/dev/null 2>&1 || kill -TERM "${main_pid}" 2>/dev/null || true
}
trap stop_main TERM INT
main_rc=0
wait "${main_pid}" || main_rc=$?
# A trapped signal returns wait early; wait again for psql itself to finish.
while kill -0 "${main_pid}" 2>/dev/null; do
    wait "${main_pid}" || main_rc=$?
done
trap - TERM INT

log "Block processing loop exited."
exit "${main_rc}"
