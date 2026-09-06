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

print_help() {
    # See scripts/install_app.sh for why the markers carry a character class.
    sed -n '/--- help-sta[r]t ---/,/--- help-en[d] ---/p' "$0" | sed 's/#\( \|$\)//; s/ *# --- help-[a-z]* ---//'
}

for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
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
# 1. Refuse while anything is connected as a haf_fyp role.
# ---------------------------------------------------------------------------
# Not just the block processor. haf-fyp-ranker, haf-fyp-api and postgrest all hold
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
        log "           docker compose -f docker/docker-compose.yml stop \\"
        log "               haf-fyp-sync haf-fyp-ranker haf-fyp-api postgrest"
        log "       then re-run this script."
        exit 1
    fi
}

assert_no_active_sessions

# ---------------------------------------------------------------------------
# 2. Confirmation.
# ---------------------------------------------------------------------------
# Typing the database name rather than 'yes': the mistake this guards against is the
# right command against the wrong host.
if [ "$FORCE" -ne 1 ]; then
    if [ ! -t 0 ]; then
        log "ERROR: refusing to run without a TTY and without --force."
        log "       This drops schemas ${HAF_FYP_SCHEMAS} CASCADE and cannot be undone."
        exit 2
    fi
    log "About to PERMANENTLY remove haf_fyp from ${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}:"
    log "  context : ${HAF_FYP_CONTEXT}"
    log "  schemas : ${HAF_FYP_SCHEMAS} (CASCADE -- all tables and all data)"
    log "  roles   : ${HAF_FYP_ROLES}"
    printf '[haf_fyp] Type the database name to confirm: '
    read -r _reply
    if [ "${_reply}" != "${POSTGRES_DB}" ]; then
        log "Aborted -- nothing was changed."
        exit 1
    fi
    # Re-checked AFTER the prompt. haf-fyp-sync is `restart: unless-stopped`, so any
    # `docker compose up` while the operator was reading the prompt brings it back,
    # and the guard above would be describing a database that no longer looks like
    # that.
    assert_no_active_sessions
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
    _who="$(psql_cmd -Atq -c "SELECT current_user || ' on ' || current_database()" 2>/dev/null || echo "")"
    if [ -n "${_who}" ]; then
        log "       (connected as ${_who})"
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
