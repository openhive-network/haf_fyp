#!/bin/bash
# Remove haf_fyp from a HAF database (#27).
#
# There was previously no supported way to uninstall this app: the schemas, roles and
# HAF context stayed behind forever, and an operator evaluating haf_fyp as a
# haf_api_node module had no way back out. balance_tracker, haf_block_explorer and
# hivesense all ship one of these (haf_stats does not); this completes the
# install_app / process_blocks / uninstall_app verb set module files assume.
#
# THIS IS DESTRUCTIVE AND HAS NO UNDO. It drops both schemas CASCADE, taking every
# table with them -- user_interests, feed_events, the caches, and the post_candidates /
# user_interactions tables that hold the ranker's whole history and take hours to
# rebuild. There is no backup step.
#
# OPTIONS:                                                      # --- help-start ---
#   --force        Skip the interactive confirmation. Required to run without a TTY.
#   --allow-foreign-context
#                  Tear down even when another application owns the HAF context, or a
#                  context whose tables live in a schema this drops. That application
#                  loses the context, is unregistered outright if it was its only one,
#                  and loses its state table. NOT implied by --force.
#   --host=VALUE   PostgreSQL host (default: $POSTGRES_HOST, else haf)
#   --port=NUMBER  PostgreSQL port (default: $POSTGRES_PORT, else 5432)
#   --user=VALUE   PostgreSQL user (default: $POSTGRES_USER, else haf_admin)
#                  --postgres-host=, --postgres-port= and --postgres-user= are
#                  accepted as synonyms (balance_tracker's spelling)
#   --postgres-url=URI  NOT SUPPORTED YET -- recognised and refused, with a
#                  message pointing at --host/--port/--user (--url= is the same)
#   --schema=NAME  Accepted for haf_api_node module-file compatibility; ignored with
#                  a warning unless it names haf_fyp_app
#   --is_forking=  Accepted likewise; this app's context is always non-forking
#   --help,-h,-?   Show this and exit                           # --- help-end ---

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# MUST precede the source. scripts/common.sh defaults POSTGRES_USER to haf_fyp_owner
# with `: "${POSTGRES_USER:=...}"`, so a default applied afterwards would never take
# effect. haf_fyp_owner cannot do this job: dropping roles needs CREATEROLE or
# superuser, and a role cannot drop itself.
: "${POSTGRES_USER:=haf_admin}"
export POSTGRES_USER

# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# NOTE the naming asymmetry, which is the trap this file exists to get right. The HAF
# CONTEXT is named 'haf_fyp'; the SCHEMA is 'haf_fyp_app'. Upstream apps use one name
# for both (btracker_app is context and schema), so a copy of their script with the
# schema substituted calls app_remove_context('haf_fyp_app') -- and because that call
# is guarded by app_context_exists(), it silently does nothing and orphans the context
# with no error to notice.
# Honours the environment, matching docker/block-processing-healthcheck.sh. An earlier
# revision hardcoded it here while the healthcheck read ${HAF_FYP_CONTEXT:-haf_fyp}, so a
# node running a renamed context got a healthcheck that followed it and an uninstall that
# silently targeted "haf_fyp" -- orphaning the real context, the exact failure the
# name-asymmetry note above exists to prevent.
HAF_FYP_CONTEXT="${HAF_FYP_CONTEXT:-haf_fyp}"
HAF_FYP_SCHEMAS="haf_fyp_app haf_fyp_endpoints"
HAF_FYP_ROLES="haf_fyp_user haf_fyp_owner"

FORCE=0

# A SEPARATE flag from --force, and an option rather than an environment variable.
# Both halves of that matter:
#
#   not --force, because --force means "there is no TTY, do not prompt" and the docker
#   entrypoint path passes it unconditionally -- balance_tracker.yaml-shaped module files
#   run uninstall as a compose service. Folding the ownership override into it would
#   disable this guard on exactly the automated runs where nobody is reading the output.
#
#   not HAF_FYP_ALLOW_FOREIGN_CONTEXT, because an override that can be set once in a
#   compose environment or an .env file and then forgotten is a guard that has silently
#   stopped existing. This one has to be typed at the moment of the destructive act, and
#   it shows up in --help where the operator looks for it.
ALLOW_FOREIGN_CONTEXT=0

print_help() {
    # See scripts/install_app.sh for why the markers carry a character class.
    sed -n '/--- help-sta[r]t ---/,/--- help-en[d] ---/p' "$0" | sed 's/#\( \|$\)//; s/ *# --- help-[a-z]* ---//'
}

for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        --allow-foreign-context) ALLOW_FOREIGN_CONTEXT=1 ;;
        # --postgres-host is balance_tracker's spelling (its install_app.sh takes that
        # and nothing else); --host is haf_block_explorer's and hivesense's. The trio
        # this MR claims parity with does not agree with itself, so accept both rather
        # than hard-failing whichever runbook the operator copied.
        --host=*|--postgres-host=*) require_flag_value "$arg"; POSTGRES_HOST="${arg#*=}" ;;
        --port=*|--postgres-port=*) require_flag_value "$arg"; POSTGRES_PORT="${arg#*=}" ;;
        --user=*|--postgres-user=*) require_flag_value "$arg"; POSTGRES_USER="${arg#*=}" ;;

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
        # A typo in a flag to a script that drops schemas must not be shrugged off --
        # least of all a mistyped --force, which would then fall through to a prompt
        # that a compose run cannot answer.
        *) log "ERROR: '${arg}' is not a valid option"; print_help; exit 2 ;;
    esac
done

wait_for_postgres

# ---------------------------------------------------------------------------
# 0. Which server actually answered (#69).
# ---------------------------------------------------------------------------
# Asked of the SERVER rather than echoed back from this script's own variables.
# ${POSTGRES_HOST} is what was REQUESTED, and the reached server can differ without any
# flag being wrong. Measured on two live PostgreSQL servers while fixing the same
# defect in haf_stats (hive/haf_stats#75):
#
#   1. PGHOSTADDR. libpq connects to `hostaddr` when both are given and uses `host`
#      -- what -h sets -- only for authentication and TLS matching. `PGHOSTADDR=<B>
#      psql -h <A>` reached B while every message said A.
#   2. A pg_service.conf carrying `hostaddr=` does the same and leaves no environment
#      variable behind: `PGSERVICE=decoy psql -h A` reached B with PGHOSTADDR unset.
#   3. PGOPTIONS can carry `-c role=...`, which changes current_user but not
#      session_user, so a refusal that names a role can be wrong too.
#
# A literal newline, for the checks below. `$(printf '\n')` cannot carry one: command
# substitution strips trailing newlines.
_NEWLINE='
'

_server_identity() {
    # host(), not ::text: the cast keeps a /32 that means nothing here.
    #
    # The cluster id is what tells two clusters on one box apart. Over a Unix socket
    # inet_server_addr() is NULL, so without it the line is "LOCAL SOCKET:-/<db>" for
    # both -- which is where #40's empty --host= lands. pg_control_system() is
    # executable by PUBLIC. It is NOT unique across copies: a physical replica, a
    # pg_basebackup or a snapshot clone of the data directory reports the same id.
    #
    # No `|| true`: the caller tests the status. psql_cmd passes -X, so a ~/.psqlrc
    # cannot write into this answer; the checks in _resolve_target stay anyway.
    psql_cmd -Atq -c "
        SELECT coalesce(host(inet_server_addr()), 'LOCAL SOCKET')
            || ':' || coalesce(inet_server_port()::text, '-')
            || '/' || current_database()
            || ' as ' || current_user
            || CASE WHEN current_user <> session_user
                    THEN ' (current_user differs from session_user ' || session_user || ')'
                    ELSE '' END
            || ' [cluster ' || (SELECT system_identifier FROM pg_control_system()) || ']'
    "
}

# Resolved ONCE, into _reached, and reused by every later line.
_resolve_target() {
    # Status, then line count, then shape; each catches what the others miss.
    #   status -- a failing psql can still have written to stdout.
    #   line count -- -Atq on a one-row query emits exactly one line, so a second line
    #     means something other than the query spoke (and a database named with an
    #     embedded newline would put an unprefixed line in the log).
    #   shape -- the query always ends ` [cluster <digits>]`, so an answer that does not
    #     is not this query's answer.
    if ! _raw="$(_server_identity)"; then _raw=""; fi
    case "${_raw}" in
        *"${_NEWLINE}"*) _reached="" ;;
        *)               _reached="${_raw}" ;;
    esac
    if ! [[ "${_reached}" =~ " [cluster "[0-9]+"]"$ ]]; then
        _reached=""
    fi

    log "  requested: ${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB} as ${POSTGRES_USER}"
    if [ -n "${_reached}" ]; then
        log "  reached  : ${_reached}"
    else
        log "  reached  : COULD NOT ASK THE SERVER."
    fi

    # Name whatever in the environment can redirect the connection. PGSERVICEFILE alone
    # is inert (libpq reads a service file only when a service NAME is given), so it is
    # reported only alongside PGSERVICE, where it says which file to go and read.
    # Newline-joined: PGSERVICEFILE is a path and may contain spaces.
    _redirect=""
    [ -n "${PGHOSTADDR:-}" ] && _redirect="${_redirect}PGHOSTADDR=${PGHOSTADDR}${_NEWLINE}"
    [ -n "${PGSERVICE:-}" ]  && _redirect="${_redirect}PGSERVICE=${PGSERVICE}${_NEWLINE}"
    if [ -n "${PGSERVICE:-}" ] && [ -n "${PGSERVICEFILE:-}" ]; then
        _redirect="${_redirect}PGSERVICEFILE=${PGSERVICEFILE}${_NEWLINE}"
    fi
    if [ -n "${_redirect}" ]; then
        # `[ -z ] ||`, not `[ -n ] &&`: a value ending in a newline leaves an empty last
        # line, the `&&` form then returns 1 from the loop, and pipefail + set -e end
        # the script right here with no message.
        printf '%s' "${_redirect}" | while IFS= read -r _var; do
            [ -z "${_var}" ] || log "  NOTE     : ${_var}"
        done
        log "             PGHOSTADDR, or the service PGSERVICE names, can carry a hostaddr,"
        log "             which libpq connects to INSTEAD of the --host / POSTGRES_HOST"
        log "             above; those then apply only to authentication and TLS. The"
        log "             'reached' line is the server that answered."
    fi

    # A pooler that reaches PostgreSQL over the Unix socket reports LOCAL SOCKET to a
    # client that connected over TCP from another machine. PgBouncer sits in the
    # haf_api_node stack this app ships a module for.
    case "${_reached}" in
        "LOCAL SOCKET"*)
            log "  NOTE     : 'LOCAL SOCKET' is the SERVER's end of the connection, not"
            log "             yours. A pooler that reaches PostgreSQL over the socket"
            log "             reports it to a remote client too, so go by the cluster id"
            log "             (which copies of one data directory share)." ;;
    esac

    # FAIL CLOSED, like every other probe in this script. If the server cannot say what
    # it is, this run cannot name what it would destroy -- and --force would otherwise
    # go ahead on nothing but the host that was typed. exit 1 ("refused, or a probe
    # could not answer"), not 2 ("invoked wrong").
    if [ -z "${_reached}" ]; then
        log "ERROR: refusing to continue without knowing which server answered."
        log "       Every other probe here fails closed; so does this one."
        exit 1
    fi
}

# BEFORE every guard, because each of them sends the operator somewhere -- "stop these
# containers", "grant this role" -- and under a redirected connection that is the wrong
# machine, while the only host in the log would be the one they typed.
log "Target:"
_resolve_target

# ---------------------------------------------------------------------------
# 1. Refuse while anything is connected as a haf_fyp role.
# ---------------------------------------------------------------------------
# Not just the block processor. haf-fyp-ranker, haf-fyp-api and haf-fyp-postgrest all hold
# sessions against these schemas, and DROP SCHEMA ... CASCADE needs an
# AccessExclusiveLock -- with any of them live, step 4 does not fail, it BLOCKS,
# after the context has already been removed.
#
# Note what is NOT a reason: an earlier revision of this comment claimed DROP ROLE
# fails while a role has active sessions. PostgreSQL has no such check -- DROP ROLE
# fails on owned objects and held privileges, and a session belonging to a dropped
# role keeps running. The lock argument above is the whole justification, and it is
# sufficient on its own.
#
# Keyed on usename, not on `query`. pg_stat_activity hides `query` for other roles'
# backends from anyone who is not superuser or a member of pg_read_all_stats, so a
# guard that pattern-matched the query text would return zero rows for a non-superuser
# operator and wave the uninstall through while the processor was running. usename is
# visible to everyone.
#
# The one exclusion is this app's own healthcheck, which connects as haf_fyp_owner
# every 60s from haf-fyp-sync (docker/block-processing-healthcheck.sh sets
# application_name for exactly this). It holds no lock and lives for milliseconds, so
# catching one in flight should not refuse a teardown the operator has already staged.
#
# Whether application_name is masked for a non-superuser caller the way `query` is has
# NOT been verified here, and the comment above should not be read as covering it. If
# it is masked the exclusion simply stops matching and the probe gets counted -- a
# spurious REFUSAL, never a spurious proceed -- so the failure direction is safe either
# way. The documented flow runs as haf_admin, where it is visible.
check_active_sessions() {
    psql_cmd -Atq -c "
        SELECT count(*) FROM pg_stat_activity
        WHERE datname = current_database()
          AND pid <> pg_backend_pid()
          AND usename IN ('haf_fyp_owner', 'haf_fyp_user')
          AND coalesce(application_name, '') <> 'haf_fyp_health_check'"
}

report_active_sessions() {
    psql_cmd -Atq -c "
        SELECT usename || ' x' || count(*) || coalesce(' (' || string_agg(DISTINCT application_name, ', ')
               FILTER (WHERE application_name <> '') || ')', '')
        FROM pg_stat_activity
        WHERE datname = current_database()
          AND pid <> pg_backend_pid()
          AND usename IN ('haf_fyp_owner', 'haf_fyp_user')
          AND coalesce(application_name, '') <> 'haf_fyp_health_check'
        GROUP BY usename" || true
}

# Fail CLOSED on anything that is not a plain number. `[ "${x}" -ne 0 ]` with x empty
# is a syntax error, which returns 2 -- and because this is an `if` CONDITION, set -e
# does not fire and the false branch is taken. An earlier revision of this script had
# exactly that shape, so a psql that answered with nothing (a permission change, a
# connection dropped mid-query) read as "no sessions" and the script went on to drop
# everything.
assert_no_active_sessions() {
    local _active
    _active="$(check_active_sessions)" || {
        log "ERROR: could not query pg_stat_activity; refusing to continue."
        exit 1
    }
    case "${_active}" in
        ''|*[!0-9]*)
            log "ERROR: unexpected session count '${_active}'; refusing to continue."
            exit 1
            ;;
    esac
    if [ "${_active}" -ne 0 ]; then
        log "ERROR: ${_active} session(s) are still connected as a haf_fyp role:"
        report_active_sessions | while read -r line; do log "         ${line}"; done
        log "       Stop everything that talks to haf_fyp first, e.g.:"
        log "           docker compose --env-file docker/.env.local \\"
        log "               -f docker/docker-compose.yml stop \\"
        log "               haf-fyp-sync haf-fyp-ranker haf-fyp-api haf-fyp-postgrest"
        log "       (add the stack's -p if it runs under one), then re-run this script."
        log "       A stack started before #53 without -p or COMPOSE_PROJECT_NAME runs"
        log "       its old containers under the project 'docker', which stop does not"
        log "       reach: move it with scripts/upgrade_standalone_stack.sh first."
        exit 1
    fi
}

assert_no_active_sessions

# ---------------------------------------------------------------------------
# 2. No way to confirm? Refuse now.
# ---------------------------------------------------------------------------
# Where it has always been: after the session guard, before the privilege guards. It
# says the command was invoked wrong (exit 2). The inventory and the prompt themselves
# come AFTER the privilege guards, just above step 3: a run that one of those refuses
# destroys nothing, so it must not print "About to PERMANENTLY remove".
if [ "$FORCE" -ne 1 ] && [ ! -t 0 ]; then
    log "ERROR: refusing to run without a TTY and without --force."
    log "       This drops schemas ${HAF_FYP_SCHEMAS} CASCADE and cannot be undone."
    exit 2
fi

# ---------------------------------------------------------------------------
# 3-5. Teardown.
# ---------------------------------------------------------------------------
# ON_ERROR_STOP=off on the destructive statements, matching balance_tracker,
# haf_block_explorer and hivesense. That is not laziness in those scripts and it is
# not here: with ON_ERROR_STOP=on (which scripts/common.sh sets) plus `set -e`, a
# failure on the FIRST schema aborts the script and leaves the context gone,
# haf_fyp_app gone, haf_fyp_endpoints present and both roles alive -- a state nothing
# recovers, because install_app.sh's precondition only inspects the context owner.
# Pushing through and then VERIFYING (step 6) reports the same failure without
# stranding a half-removed app.
#
# lock_timeout so a DROP that cannot get its lock fails in 30s with a clear error
# rather than blocking forever behind a session that appeared after step 1.
_destructive_errors=0

_psql_destructive() {
    # `|| _destructive_errors=...`, NOT a bare call. ON_ERROR_STOP alone does NOT do
    # what this design needs, which was verified against a live PostgreSQL 14 rather
    # than assumed:
    #
    #   -c failing LAST,  ON_ERROR_STOP=off -> exit 1
    #   -c failing FIRST, ON_ERROR_STOP=off -> exit 0
    #   -f  failing,      ON_ERROR_STOP=off -> exit 0
    #   -f  failing,      ON_ERROR_STOP=on  -> exit 3
    #
    # ON_ERROR_STOP controls whether psql CONTINUES to later actions; the exit status
    # is the LAST action's result. Here the failing statement is the last -c, so psql
    # exits 1 -- and as a bare top-level command under `set -e` that killed the script
    # at the first destructive error, before step 6 could report anything. The state it
    # left behind (context gone, one schema gone, roles alive, no log line) is exactly
    # what the push-through-and-verify design exists to prevent.
    #
    # balance_tracker and hivesense both put `|| true` on their destructive psql for
    # this reason; an earlier revision here copied their ON_ERROR_STOP=OFF and dropped
    # the half that makes it work. Counting rather than `|| true` so step 6 can tell
    # "a statement errored" from "the verification found survivors".
    if ! psql_cmd -v ON_ERROR_STOP=off -c "SET lock_timeout = '30s';" -c "$1"; then
        _destructive_errors=$((_destructive_errors + 1))
        log "  (statement failed; continuing -- step 6 reports what survived)"
    fi
}

# 3. The HAF context, FIRST -- while its schema still exists. hive.app_remove_context
#    drops the context's own table and views, which live in the application schema, so
#    doing this after DROP SCHEMA CASCADE would leave hafd.contexts pointing at objects
#    that are already gone. It does not detach first, but it does not need to:
#    hive.context_remove deletes the hafd.contexts_attachment row itself.
# hive.context_remove is NOT SECURITY DEFINER and hafd.contexts has RLS whose DELETE
# policy is USING (hive.can_impersonate(current_user, owner)). A caller who is neither
# superuser nor able to impersonate haf_fyp_owner therefore deletes ZERO ROWS WITHOUT
# ERROR: app_remove_context returns success, this script logs "removed context", and the
# context survives while both schemas are dropped. --user is an advertised flag, so that
# is reachable by an operator following the help text. install_app.sh's step 10 documents the
# same RLS trap on the creation side and works around it with SET ROLE.
#
# (Step 6 DOES report the survivor -- see the policy quote below. What it cannot do is
# report it before the schemas are gone.)
#
# WHY IT REFUSES, corrected. An earlier revision said step 6 could not catch the
# orphan because it "reads through the same filter". That is false, and it was the
# justification for the whole change, so it is worth being exact. From hive/haf,
# src/hive_fork_manager/authorization.sql:
#
#     CREATE POLICY sp_applications_hive_context        ON hafd.contexts
#       FOR SELECT TO hive_applications_group USING( TRUE );
#     CREATE POLICY sp_applications_delete_hive_context ON hafd.contexts
#       FOR DELETE TO hive_applications_group USING( hive.can_impersonate(current_user, owner) );
#
# SELECT and DELETE are SEPARATE policies. Only the DELETE is filtered. So step 6 does
# see the surviving row, and the pre-MR behaviour was INCOMPLETE + exit 1, not
# "haf_fyp removed" + exit 0.
#
# Refusing is still right, for a different reason: the alternative is not a silent
# success, it is a LOUD FAILURE THAT ARRIVES TOO LATE -- after both DROP SCHEMA
# CASCADEs. Failing here leaves the operator somewhere recoverable.
#
# ASK THE QUESTION THE POLICY ASKS. The predicate used to test membership of the literal
# 'haf_fyp_owner'; the policy tests can_impersonate(current_user, owner) where `owner` is
# the ROW's column. install_app.sh's step 10 documents the case where they differ -- a
# context registered by an older build is owned by haf_admin, not haf_fyp_owner -- and
# membership of the literal answers TRUE there while the DELETE still matches nothing.
# Reading the row also subsumes the to_regrole dance (pg_has_role RAISES 42704 on an
# unknown role, which mattered because this script advertises idempotent re-runs), the
# already-removed context, and the database that never had HAF.
# Report the role the database actually sees. ${POSTGRES_USER} is what was REQUESTED;
# every probe above correctly asks about current_user, and the two can differ (a
# PGUSER/PGSERVICE in the environment, a .pgpass-driven fallback, a service file). A
# refusal that names the wrong role sends the operator to change the wrong thing.
_report_connected_identity() {
    # What section 0 resolved: the server's address, database, role and cluster id.
    # It used to ask again for only `current_user on current_database()`, which over a
    # Unix socket is identical for two clusters on one box.
    if [ -n "${_reached:-}" ]; then
        log "       (connected to ${_reached})"
    fi
}

# GUARD 1 -- is this a HAF database at all?
#
# pg_class/pg_namespace, NOT to_regclass('hafd.contexts'). to_regclass RESOLVES THE
# SCHEMA, and aclcheck_error fires even under missing_ok, so it raises
# `permission denied for schema hafd` for any caller without USAGE on it -- verified on
# postgres:14 and postgres:17. That made the script announce "this is not a HAF
# instance" about a database that plainly is one, sending the operator to diagnose the
# wrong problem. This is the same trap step 6 already documents and works around for
# `cron`; it was left in the pre-flight. A catalog join needs no aclcheck.
_has_contexts="$(psql_cmd -Atq -c "
    SELECT EXISTS (SELECT 1 FROM pg_class c
                   JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname = 'hafd' AND c.relname = 'contexts')")" || {
    log "ERROR: could not query the catalog to check whether this is a HAF instance."
    log "       Nothing has been changed. See the psql error above."
    exit 1
}

# GUARD 2 -- may this role delete the context row?
_can_remove="t"
if [ "${_has_contexts}" = "t" ]; then
    # A separate statement, not an OR arm: a WHERE cannot protect a FROM, because
    # PostgreSQL resolves relation names at parse analysis. Step 6 splits its probes for
    # the same reason.
    #
    # POSITIVE PROOF OF VISIBILITY, not inference from absence. An earlier revision was
    # `superuser OR NOT EXISTS(row) OR EXISTS(row AND can_impersonate)`, and the middle
    # arm cannot tell "the context is gone" from "the context is INVISIBLE TO ME under
    # RLS" -- it reads the row through the very policy in question. A role holding
    # SELECT on hafd but outside hive_applications_group (which is exactly what
    # db/builtin_roles.sql creates) saw zero rows and passed vacuously; then
    # hive.app_context_exists() read through the same filter and reported nothing to
    # remove, the schemas were dropped, and step 6's probe -- same filter again --
    # printed success while the context survived. Requiring group membership first turns
    # that state into a refusal.
    #
    # to_regrole guard: pg_has_role RAISES 42704 on an unknown role. The group exists on
    # any real HAF database, and this branch only runs when hafd.contexts does, but the
    # guard costs nothing and this script advertises idempotent re-runs.
    #
    # NOT wrapped in `2>/dev/null || echo ""`: that collapsed a connection reset, a
    # permission error and a genuine "no" into one confident and possibly wrong message.
    if ! _can_remove="$(psql_cmd -Atq -c "
        SELECT current_setting('is_superuser') = 'on'
            OR (to_regrole('hive_applications_group') IS NOT NULL
                AND pg_has_role(current_user, 'hive_applications_group', 'USAGE')
                AND (NOT EXISTS (SELECT 1 FROM hafd.contexts WHERE name = '${HAF_FYP_CONTEXT}')
                     OR EXISTS (SELECT 1 FROM hafd.contexts
                                WHERE name = '${HAF_FYP_CONTEXT}'
                                  AND hive.can_impersonate(current_user, owner))))")"; then
        log "ERROR: could not determine whether this role may remove the HAF context."
        log "       That query needs SELECT on hafd.contexts and EXECUTE on"
        log "       hive.can_impersonate, both of which come with hive_applications_group."
        log "       Nothing has been changed. See the psql error above."
        exit 1
    fi
fi

if [ "${_can_remove}" != "t" ]; then
    # REFUSE. hive.app_remove_context drops the context's views and its state table
    # BEFORE the DELETE (hive/haf app_api.sql, context_rewind/rewind_api.sql), so a
    # caller the DELETE policy filters out still destroys objects and then deletes zero
    # rows without error.
    #
    # BE ACCURATE ABOUT WHAT FOLLOWS. Step 6 DOES catch this: SELECT on hafd.contexts is
    # a separate policy, USING (TRUE) (authorization.sql), so the verification sees the
    # surviving row and exits 1 with INCOMPLETE. An earlier revision claimed it "reads
    # through the same filter and reports success"; that was wrong. The reason to refuse
    # is that the alternative is not a silent success but A LOUD FAILURE THAT ARRIVES
    # AFTER both schemas have been dropped.
    log "ERROR: this role cannot remove the HAF context '${HAF_FYP_CONTEXT}'."
    log "       hafd.contexts' DELETE policy is hive.can_impersonate(current_user, owner),"
    log "       and it also requires membership of hive_applications_group to see the row"
    log "       at all -- so the delete would affect zero rows without error, and the"
    log "       teardown would drop both schemas before step 6 reported the orphan."
    log "       Re-run as haf_admin. Nothing has been changed."
    _report_connected_identity
    exit 1
fi

# GUARD 3 -- may this role complete step 5?
#
# Step 5 runs DROP OWNED BY and then DROP ROLE, and CREATEROLE is NOT the question
# either of them asks. Verified on postgres:14 and postgres:17, as a non-superuser
# CREATEROLE role that is not a member of the target:
#
#     DROP OWNED BY haf_fyp_owner CASCADE;
#       ERROR: permission denied to drop objects
#       DETAIL: Only roles with privileges of role "haf_fyp_owner" may drop objects owned by it.
#     DROP ROLE haf_fyp_user;
#       ERROR: permission denied to drop role
#       DETAIL: Only roles with the CREATEROLE attribute and the ADMIN option on role
#               "haf_fyp_user" may drop this role.
#
# The second rule is PG16+, and HAF's own Dockerfile pins POSTGRES_VERSION=17, so it is
# the live rule -- not a hypothetical. An earlier revision of this guard tested
# rolcreaterole alone, which answered TRUE for exactly the role that then destroyed
# everything and failed at step 5's first statement: the outcome this guard was added to
# prevent. Its error message also asserted that CREATEROLE was sufficient, which is
# false on both versions.
#
# So ask for what step 5 needs, per role it will drop: USAGE (has_privs_of_role, which
# is what DROP OWNED BY checks) and MEMBER WITH ADMIN OPTION (what DROP ROLE checks).
# Requiring the admin option is slightly stricter than PG14 demands; that costs nothing,
# because the documented and supported connection is haf_admin, which HAF creates as a
# superuser and short-circuits the first arm.
_can_drop_roles="$(psql_cmd -Atq -c "
    SELECT current_setting('is_superuser') = 'on'
        OR (COALESCE((SELECT rolcreaterole FROM pg_roles WHERE rolname = current_user), false)
            AND NOT EXISTS (
                SELECT 1 FROM pg_roles r
                WHERE r.rolname IN ('haf_fyp_user', 'haf_fyp_owner')
                  AND NOT (pg_has_role(current_user, r.oid, 'USAGE')
                           AND pg_has_role(current_user, r.oid, 'MEMBER WITH ADMIN OPTION'))))")" || {
    log "ERROR: could not determine whether this role may drop haf_fyp's roles."
    log "       Nothing has been changed. See the psql error above."
    exit 1
}
if [ "${_can_drop_roles}" != "t" ]; then
    log "ERROR: this role cannot complete the teardown's final step, so the teardown is"
    log "       refused before anything is dropped."
    log "       Step 5 runs DROP OWNED BY -- which needs the PRIVILEGES OF haf_fyp_user and"
    log "       haf_fyp_owner, not CREATEROLE -- and DROP ROLE, which on PostgreSQL 16+ needs"
    log "       CREATEROLE *and* the ADMIN option on each role. Without both, the context"
    log "       and both schemas would be dropped and step 5 would then fail."
    log "       Re-run as haf_admin (a superuser), or grant this role ADMIN option on"
    log "       haf_fyp_user and haf_fyp_owner. Nothing has been changed."
    _report_connected_identity
    exit 1
fi

# GUARD 4 -- is this context ours to take apart? (#66)
#
# From haf#341 a context can belong to an APPLICATION, and from haf#345 an application
# may hold the contexts of one it embeds. In that arrangement both destructive steps
# below reach into somebody else's app:
#
#   step 3  hive.app_remove_context routes through hive.app_registry_context_removed,
#           which strips the context from the OWNING application's contexts array and
#           app_unregisters that application outright when it was its only one.
#   step 4  DROP SCHEMA haf_fyp_app CASCADE takes the context's own state table and
#           views with it -- they live in the app's schema -- while that application's
#           driver is still calling hive.app_next_iteration on the context.
#
# Neither is recoverable by re-running anything here, and the operator asked to remove
# THIS app, not to stop another one. So refuse before the first destructive statement,
# which is where this script puts every other unrecoverable answer.
#
# Existence via pg_class, not to_regclass: Guard 1 records why (to_regclass resolves the
# schema and aclcheck_error fires even under missing_ok, so it raises for a caller
# without USAGE on hafd). A HAF that predates haf#341 has no hafd.applications at all,
# and on such a node no application can own anything -- skip, do not refuse.
#
# The APPLICATION is 'haf_fyp' whatever the context is called, so the probe excludes that
# name and nothing else. Being exact about HAF_FYP_CONTEXT, because the note at the top of
# this file is easy to over-read: it does not rename the context in the database.
# db/haf_fyp_app.sql creates the context as 'haf_fyp', db/register_app.sql registers the
# application as 'haf_fyp' and scripts/process_blocks.sh drives 'haf_fyp', all hardcoded.
# Only this script and docker/block-processing-healthcheck.sh read the variable, so it
# retargets a teardown -- which is exactly why the schema half of the probe matters: set
# it to something the database does not have and the name half matches nothing, while
# step 4 still drops the schema the real context lives in.
if [ "${_has_contexts}" = "t" ]; then
    if ! _has_applications="$(psql_cmd -Atq -c "
        SELECT EXISTS (SELECT 1 FROM pg_class c
                       JOIN pg_namespace n ON n.oid = c.relnamespace
                       WHERE n.nspname = 'hafd' AND c.relname = 'applications')")"; then
        log "ERROR: could not query the catalog for the HAF application registry."
        log "       Nothing has been changed. See the psql error above."
        exit 1
    fi

    if [ "${_has_applications}" = "t" ]; then
        # The query is scripts/uninstall_foreign_context.sql, not an inline -c, for the
        # reason the verification is a file: it decides whether to destroy anything, so it
        # has to be runnable on its own. tests/integration/test_uninstall_foreign_context_db.py
        # executes THAT FILE against a real PostgreSQL with rows in it -- which is the only
        # way to tell `a.name <> 'haf_fyp'` from `a.name <> a.name`, or `@>` from `<@`. A
        # stub psql that answers by matching the table name cannot, and did not.
        #
        # It asks by context name AND by schema, because the two destructive steps are
        # keyed differently and both reach a foreign application; the file says which.
        _schema_list="$(printf '%s' "${HAF_FYP_SCHEMAS}" | tr ' ' ',')"

        # Not `2>/dev/null || echo ""`, for Guard 2's reason: that collapses a
        # connection reset and a permission error into a confident "nobody owns it".
        if ! _context_owners="$(psql_cmd -Atq \
            -v context="${HAF_FYP_CONTEXT}" -v schemas="${_schema_list}" \
            -f "${SCRIPT_DIR}/uninstall_foreign_context.sql")"; then
            log "ERROR: could not determine which application owns the HAF context."
            log "       That query needs SELECT on hafd.applications, which comes with"
            log "       hive_applications_group. Nothing has been changed. See the psql"
            log "       error above."
            exit 1
        fi

        if [ -n "${_context_owners}" ] && [ "${ALLOW_FOREIGN_CONTEXT}" -eq 1 ]; then
            # The escape hatch. haf#345 makes a foreign owner a LEGITIMATE arrangement --
            # a parent application deliberately holds the contexts of the apps it embeds
            # -- and an operator tearing that parent down knows it. Refusing outright left
            # them nothing but the hand-written SQL below, on a teardown that every other
            # HAF app performs unconditionally (balance_tracker, haf_block_explorer and
            # hivesense all call app_remove_context behind nothing but app_context_exists).
            # So the default stays safe and the informed operator gets a way through.
            #
            # Still printed in full, and still naming every owner: an override says the
            # operator accepted this, not that they should stop being told what it costs.
            log "WARNING: --allow-foreign-context was given, so this is going ahead."
            log "         These applications own a HAF context this teardown is about to"
            log "         destroy -- '${HAF_FYP_CONTEXT}' itself, or a context whose tables"
            log "         live in a schema this script drops:"
            printf '%s\n' "${_context_owners}" | while IFS= read -r _owner; do
                [ -n "${_owner}" ] || continue
                log "           ${_owner}"
            done
            log "         Each one loses this context, is unregistered outright if it was"
            log "         its only context, and loses that context's state table to DROP"
            log "         SCHEMA CASCADE while its driver is still calling"
            log "         hive.app_next_iteration on it. Re-installing haf_fyp does not"
            log "         undo any of it."
            log "         See README, \"If another application owns the context\"."
        elif [ -n "${_context_owners}" ]; then
            # One name per line, and each gets its own remedy. Asking by schema as well as
            # by name made more than one owner reachable -- two contexts in haf_fyp_app,
            # owned by different applications -- and a comma-joined list interpolated into
            # `WHERE name = '...'` would have printed SQL that does not run.
            log "ERROR: these applications own a HAF context this teardown would destroy"
            log "       -- '${HAF_FYP_CONTEXT}' itself, or a context whose tables live in a"
            log "       schema this script drops:"
            printf '%s\n' "${_context_owners}" | while IFS= read -r _owner; do
                [ -n "${_owner}" ] && log "         ${_owner}"
            done
            log "       Removing the context strips it from that application -- and"
            log "       unregisters the application entirely if it was its only context --"
            log "       and DROP SCHEMA CASCADE takes the context's state table with it"
            log "       while that application is still processing blocks."
            log "       Nothing has been changed."
            log ""
            # The obvious remedy is wrong twice over, so spell out the one that is not.
            # hive.app_register(name, fewer_contexts) ALSO sets process_procedure and
            # completed_block_function from its arguments (ON CONFLICT DO UPDATE), so the
            # detach silently blanks the other application's driver wiring -- and with an
            # empty group it does nothing at all, because CARDINALITY(contexts) > 0 is a
            # CHECK and `contexts @> '{}'` matches every other row, which sends
            # app_register down its quiet "already part of another application" return.
            log "       Where an application has other contexts, detach this one without"
            log "       touching the rest of its registration (as haf_admin, or a role that"
            log "       can impersonate its owner):"
            printf '%s\n' "${_context_owners}" | while IFS= read -r _owner; do
                [ -n "${_owner}" ] || continue
                log "         UPDATE hafd.applications"
                log "            SET contexts = array_remove(contexts, '${HAF_FYP_CONTEXT}')"
                log "          WHERE name = '${_owner}';"
            done
            log ""
            log "       Where this is an application's ONLY context there is no detach --"
            log "       hafd.applications requires at least one -- so removing haf_fyp here"
            log "       means removing that application too:"
            printf '%s\n' "${_context_owners}" | while IFS= read -r _owner; do
                [ -n "${_owner}" ] && log "         SELECT hive.app_unregister('${_owner}');"
            done
            log "       It keeps its position (hafd.contexts is untouched), but every"
            log "       dependency row it takes part in is deleted, in BOTH directions, and"
            log "       re-registering later does not bring them back."
            log ""
            # Named here because this is where the operator who meant it is standing. An
            # embedding application holding this context is a supported arrangement since
            # haf#345, not necessarily a mistake -- the refusal is the default, not a
            # verdict.
            log "       If that other application is yours and this is deliberate -- an"
            log "       application that embeds haf_fyp, being torn down with it -- re-run"
            log "       with --allow-foreign-context to accept all of the above and"
            log "       continue. It is not implied by --force."
            log ""
            log "       See README, \"If another application owns the context\"."
            _report_connected_identity
            exit 1
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Confirmation. Every guard above has passed, so destruction is actually next.
# ---------------------------------------------------------------------------
# The privilege guards are NOT re-run after the prompt (only the session guard is).
# An accepted gap: a privilege revoked while the operator reads the prompt is not
# noticed until a statement fails, and step 6 then reports what is left.
#
# What the server line does not promise: each psql call below opens its own
# connection with the same parameters. With a multi-host POSTGRES_HOST, DNS
# round-robin or a pooler with several backends, a later call can reach a different
# server than the one that answered section 0.
# Printed on BOTH paths (#69). It used to sit inside the no-force branch, and
# docker/docker-compose.yml ships --force: the unattended run, the one most likely to
# be pointed somewhere unexpected by its environment, left no record of what it
# destroyed or where. The server line is what the server answered, not ${POSTGRES_HOST}.
log "About to PERMANENTLY remove haf_fyp:"
log "  server  : ${_reached}"
log "  context : ${HAF_FYP_CONTEXT}"
log "  schemas : ${HAF_FYP_SCHEMAS} (CASCADE -- all tables and all data)"
log "  roles   : ${HAF_FYP_ROLES}"

if [ "$FORCE" -ne 1 ]; then
    # Typing the database name rather than 'yes': the mistake this guards against is the
    # right command against the wrong host. Compared against what the SERVER answered,
    # not against ${POSTGRES_DB}: that form compared the reply with this script's own
    # variable, so it could only confirm that the operator can read. It still cannot
    # tell apart two servers holding databases of the same name, which is what the
    # server line above is for.
    #
    # The status is tested, not discarded: a failed query must reach the operator as
    # "could not ask the server", not as "Aborted" after their reply fails to match noise.
    if ! _target_db="$(psql_cmd -Atq -c "SELECT current_database()")"; then
        _target_db=""
    fi
    case "${_target_db}" in
        "" | *"${_NEWLINE}"*)
            log "ERROR: could not ask the server which database this is; refusing to prompt."
            exit 1 ;;
    esac
    printf '[haf_fyp] Type the database name to confirm: '
    read -r _reply
    if [ "${_reply}" != "${_target_db}" ]; then
        log "Aborted -- nothing was changed."
        exit 1
    fi
    # Re-checked AFTER the prompt. haf-fyp-sync is `restart: unless-stopped`, so any
    # `docker compose up` while the operator was reading the prompt brings it back,
    # and the guard above would be describing a database that no longer looks like
    # that.
    assert_no_active_sessions
fi

log "Removing HAF context '${HAF_FYP_CONTEXT}'..."
_psql_destructive "
DO \$\$
BEGIN
    IF to_regclass('hafd.contexts') IS NULL THEN
        RAISE NOTICE 'not a HAF database (no hafd.contexts) - skipping context removal';
        RETURN;
    END IF;
    IF hive.app_context_exists('${HAF_FYP_CONTEXT}') THEN
        PERFORM hive.app_remove_context('${HAF_FYP_CONTEXT}');
        RAISE NOTICE 'removed context ${HAF_FYP_CONTEXT}';
    ELSE
        RAISE NOTICE 'no context named ${HAF_FYP_CONTEXT} - nothing to remove';
    END IF;
END\$\$;"

# 4. Schemas.
for _schema in ${HAF_FYP_SCHEMAS}; do
    log "Dropping schema ${_schema} CASCADE..."
    _psql_destructive "DROP SCHEMA IF EXISTS ${_schema} CASCADE;"
done

# 5. Roles, last and conditionally. The guard reads hafd.contexts rather than
#    assuming: if any context is still owned by haf_fyp_owner, dropping the role would
#    break that instead of this one. DROP OWNED BY is still needed after DROP SCHEMA --
#    db/cross_schema_grants.sql gives these roles privileges on hivesense_app,
#    reptracker_app, btracker_app and hafd, and DROP ROLE fails while any remain.
log "Dropping roles ${HAF_FYP_ROLES}..."
_psql_destructive "
DO \$\$
BEGIN
    -- NESTED, not a single condition of the form
    --     to_regclass(...) IS NOT NULL AND EXISTS (SELECT ... FROM hafd.contexts)
    -- (and note: no backticks in here -- this SQL is a double-quoted bash string, so a
    -- backtick would be command substitution, which is a shell error, not a comment).
    -- PL/pgSQL plans an IF condition as one SQL expression, so the sub-select's FROM
    -- clause is resolved at parse time and the AND does not protect it -- name
    -- resolution is not short-circuited by runtime operators. An outer IF whose body
    -- is a separate statement is: plpgsql plans statements lazily, so the inner query
    -- is never planned when the outer test fails. That is why step 3's guard works and
    -- why this one had to be rewritten to look like it.
    IF to_regclass('hafd.contexts') IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM hafd.contexts WHERE owner = 'haf_fyp_owner') THEN
            RAISE NOTICE 'haf_fyp_owner still owns a HAF context - leaving both roles in place';
            RETURN;
        END IF;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'haf_fyp_user') THEN
        DROP OWNED BY haf_fyp_user CASCADE;
        DROP ROLE haf_fyp_user;
        RAISE NOTICE 'dropped role haf_fyp_user';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'haf_fyp_owner') THEN
        DROP OWNED BY haf_fyp_owner CASCADE;
        DROP ROLE haf_fyp_owner;
        RAISE NOTICE 'dropped role haf_fyp_owner';
    END IF;
END\$\$;"

# ---------------------------------------------------------------------------
# 6. Verify, and say exactly what is left.
# ---------------------------------------------------------------------------
# The whole reason step 3-5 can push past an error. Without this the permissive
# ON_ERROR_STOP would turn a partial teardown into a silent success -- which is the
# failure mode upstream's scripts DO have, and the one thing worth adding to them.
log "Verifying..."
# Split in two, because a WHERE clause cannot protect a FROM clause. An earlier
# revision wrote
#     SELECT ... FROM hafd.contexts WHERE to_regclass('hafd.contexts') IS NOT NULL ...
# and claimed that guarded the non-HAF case. It does not: PostgreSQL resolves relation
# names during parse analysis, before any predicate runs, so the statement still failed
# with `relation "hafd.contexts" does not exist` -- and because psql_cmd inherits
# ON_ERROR_STOP=on, the failing command substitution aborted the script under `set -e`
# with no log line at all, after everything had already been dropped. The one property
# this step exists to provide -- saying what survived -- was exactly what was lost.
#
# pg_namespace and pg_roles are catalog tables that always exist, so only the context
# branch needs gating, and it is gated by a separate statement that decides whether to
# run it at all.
# Every probe below captures its own failure. A command substitution inside an `if`
# CONDITION is exempt from `set -e`, so `if [ "$(psql ...)" = "t" ]` on a psql that
# failed yields "" -- not "t" -- and the context branch is skipped SILENTLY, leaving a
# registered context while the script prints "haf_fyp removed" and exits 0. That is the
# orphaned-context outcome this file's header says it exists to prevent, arrived at by
# a different route. Assignments outside a condition are NOT exempt, hence the explicit
# `|| _probe_failed=1` on each.
_probe_failed=0

_remaining="$(psql_cmd -Atq -f "${SCRIPT_DIR}/uninstall_verify.sql")" || _probe_failed=1
# Same catalog join as guard 1, for the same reason: to_regclass raises rather than
# returning NULL when the caller lacks USAGE on hafd.
_has_contexts="$(psql_cmd -Atq -c "
    SELECT EXISTS (SELECT 1 FROM pg_class c
                   JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname = 'hafd' AND c.relname = 'contexts')")" || _probe_failed=1

if [ "${_has_contexts}" = "t" ]; then
    _context_left="$(psql_cmd -Atq -c \
        "SELECT 'context ' || c.name FROM hafd.contexts AS c WHERE c.name = '${HAF_FYP_CONTEXT}'")" \
        || _probe_failed=1
    if [ -n "${_context_left}" ]; then
        _remaining="${_context_left}${_remaining:+, }${_remaining}"
    fi
fi

if [ "${_probe_failed}" -ne 0 ]; then
    log "ERROR: could not verify the teardown -- a probe query failed."
    log "       Do NOT assume it succeeded. Re-run this script; it is idempotent."
    exit 1
fi

if [ -n "${_remaining}" ]; then
    log "INCOMPLETE: the following still exist: ${_remaining}"
    log "            Re-running this script is safe and will retry them. If a role"
    log "            survives, something outside this database still owns objects it"
    log "            holds privileges on; see the psql errors above."
    exit 1
fi

if [ "${_destructive_errors}" -ne 0 ]; then
    # Nothing survived, so the teardown did complete -- but statements errored on the
    # way, and silently reporting success would hide that from whoever has to explain
    # the log later.
    log "NOTE: ${_destructive_errors} statement(s) errored during teardown, but nothing"
    log "      remains. See the psql errors above."
fi

log "haf_fyp removed. scripts/install_app.sh recreates it from scratch."
log "The cross-schema grants on hivesense_app / reptracker_app / btracker_app went with"
log "the roles: db/cross_schema_grants.sql grants them TO haf_fyp_user/haf_fyp_owner, so"
log "DROP OWNED BY ... CASCADE revoked them -- which is why step 5 needs it at all."
log "NOT removed, because this app did not create it: the pgvector extension."
