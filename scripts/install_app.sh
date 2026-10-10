#!/bin/bash
# One-time schema installation. Run once against a live HAF database.
# Safe to re-run: all DDL uses IF NOT EXISTS / ON CONFLICT DO NOTHING.
#
# PREREQUISITE: pgvector must be installed on the PostgreSQL server:
#   Run as superuser: CREATE EXTENSION IF NOT EXISTS vector;
#
# OPTIONS:                                                      # --- help-start ---
#   --swagger-url=BASE  Base URL the OpenAPI document advertises in `servers`: an
#                       absolute path (/haf-fyp-api, or / on a standalone stack) or an
#                       http(s) URL with a path. Anything else, a bare hostname
#                       included, is ignored with a warning. Empty, or not given,
#                       puts the default back. Default: /haf-fyp-api
#   --host=VALUE        PostgreSQL host  (default: $POSTGRES_HOST, else haf)
#   --port=NUMBER       PostgreSQL port  (default: $POSTGRES_PORT, else 5432)
#   --user=VALUE        PostgreSQL user  (default: $POSTGRES_USER, else haf_fyp_owner)
#                       --postgres-host=, --postgres-port= and --postgres-user= are
#                       accepted as synonyms, because that is balance_tracker's
#                       spelling and runbooks get copied
#   --is_forking=FALSE  Accepted for compatibility with the upstream module template.
#                       TRUE is warned about and ignored -- see below.
#   --schema=NAME       Accepted likewise. Anything but haf_fyp_app is warned about
#                       and ignored; the schema names are fixed in the SQL.
#   --postgres-url=URI  NOT SUPPORTED YET -- recognised and refused with a message
#                       pointing at --host/--port/--user. --url= is the same flag.
#   --help,-h,-?        Show this and exit                       # --- help-end ---

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# Empty means "not given": the document then advertises its default base URL.
SWAGGER_URL=""

print_help() {
    # Delimited by the markers above rather than a hardcoded line range. A range
    # (`sed -n '2,12p'`) silently truncates as soon as the header grows by a line,
    # and a test asserting on one early flag never notices.
    #
    # The bracket in help-sta[r]t is not decoration: written literally, the pattern
    # matches THIS LINE too, sed re-opens the range here and prints the rest of the
    # file. The character class matches the marker above without matching itself.
    sed -n '/--- help-sta[r]t ---/,/--- help-en[d] ---/p' "$0" | sed 's/#\( \|$\)//; s/ *# --- help-[a-z]* ---//'
}

# The connection flags are parsed AFTER common.sh has applied its defaults, and every
# psql_cmd call expands them at call time -- so assigning here genuinely overrides the
# environment rather than being overridden by it.
for arg in "$@"; do
    case "$arg" in
        --swagger-url=*) SWAGGER_URL="${arg#*=}" ;;

        # Matching balance_tracker, haf_block_explorer and hivesense, all of which
        # accept these on install_app and uninstall_app and whose docker entrypoints
        # forward them. Without them the documented upstream operator form,
        # `run --rm <svc> install_app --host=haf`, fails on an app claiming to
        # implement the same contract.
        # --postgres-host is balance_tracker's spelling and the only connection flag
        # its install_app.sh takes; --host is haf_block_explorer's and hivesense's.
        # The trio does not agree, so both are accepted -- otherwise the new
        # unknown-option error below turns a copied balance_tracker runbook into a
        # hard install failure, which under haf-fyp-setup's restart: on-failure is
        # the same crash-loop the --is_forking handling was softened to avoid.
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

        # haf_api_node's module template passes --is_forking defaulted from a node-wide
        # ${IS_FORKING:-TRUE}, so a haf_fyp module file copied from an upstream app
        # carries TRUE without anyone choosing it for this app. It cannot be honoured:
        # create_context_if_not_exists() calls hive.app_context_set_non_forking('haf_fyp')
        # when it CREATES the context (db/haf_fyp_app.sql), so HAF does not rewind
        # these tables on a fork.
        #
        # Note the limit of that, since an earlier revision of this comment said
        # "unconditionally": the call sits inside the `IF NOT app_context_exists`
        # branch. On a database whose context already exists -- created forking by an
        # older build or by hand -- this warning is still correct that the flag is not
        # honoured, but nothing here converts that context to non-forking either.
        #
        # WARN, do not fail. An earlier revision of this MR exited 2 here, which was
        # wrong in a way worth recording: haf-fyp-setup carries `restart: on-failure`
        # while every other service waits on `service_completed_successfully`, so a hard
        # failure on an inherited node-wide default would crash-loop the installer and
        # keep the whole stack from ever starting -- over a flag that is a no-op here.
        --is_forking=*)
            case "${arg#*=}" in
                FALSE|false|False)
                    log "--is_forking=FALSE matches this app's context; nothing to do."
                    ;;
                *)
                    log "WARNING: --is_forking=${arg#*=} is ignored."
                    log "         haf_fyp registers a NON-forking HAF context by design"
                    log "         (hive.app_context_set_non_forking, db/haf_fyp_app.sql), so HAF"
                    log "         does NOT rewind its tables on a fork. Continuing non-forking."
                    log "         Pass --is_forking=FALSE to silence this."
                    ;;
            esac
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

        --help|-h|-\?) print_help; exit 0 ;;

        # Unknown arguments are an ERROR, matching upstream's option loops. This loop
        # used to have no fallthrough at all, so a mistyped --swagger-url was silently
        # dropped, with nothing in the log to say so.
        *)
            log "ERROR: '${arg}' is not a valid option"
            print_help
            exit 2
            ;;
    esac
done

wait_for_postgres

# HAF's install lock (#67). A block processor holds a SHARED advisory lock on haf_fyp for
# as long as it runs (haf_fyp_app.take_block_processor_locks, and the driver's --lock);
# this takes it EXCLUSIVELY, so the install cannot run over a live processor -- which,
# before, it simply raced: functions re-created and the context pointer seeded under a
# loop mid-range.
#
# The lock is session-level, and this script runs many psql sessions, so it is held by
# the psql base image's install_with_app_lock.py on a connection of its own, which then
# runs this script again as its child. The re-run sees HAF_INSTALL_LOCK_HELD and goes
# straight on. When a processor holds the lock the wrapper logs who, skips the install
# and exits 0 -- so haf-fyp-setup completes and the running stack is left as it is. Stop
# the block processor before re-installing (README). The same wrapper and the same flag
# as balance_tracker, reputation_tracker, haf_block_explorer, hivesense and nft_tracker.
#
# Without the wrapper (a checkout run by hand, CI) or on a HAF without the lock functions
# (the wrapper handles that one itself), the install runs unlocked, as it always has.
INSTALL_LOCK_WRAPPER="${HAF_FYP_INSTALL_LOCK_WRAPPER:-/usr/local/bin/install_with_app_lock.py}"

# One libpq keyword value, quoted: a space or quote in a value would otherwise split or end
# it. Inside single quotes libpq takes \' and \\ as escapes.
dsn_value() {
    local v="${1//\\/\\\\}"
    printf "'%s'" "${v//\'/\\\'}"
}

if [ -z "${HAF_INSTALL_LOCK_HELD:-}" ]; then
    export HAF_INSTALL_LOCK_HELD=1
    if command -v python3 >/dev/null 2>&1 && [ -f "${INSTALL_LOCK_WRAPPER}" ]; then
        log "Taking HAF's install lock on haf_fyp..."
        exec python3 "${INSTALL_LOCK_WRAPPER}" haf_fyp \
            "host=$(dsn_value "${POSTGRES_HOST}") port=$(dsn_value "${POSTGRES_PORT}") user=$(dsn_value "${POSTGRES_USER}") dbname=$(dsn_value "${POSTGRES_DB}") application_name=haf_fyp-install" \
            "$0" "$@"
    fi
    log "WARNING: ${INSTALL_LOCK_WRAPPER} not found; installing WITHOUT HAF's install lock,"
    log "         so nothing stops this install running over a live block processor."
fi

# 0. PRECONDITIONS — checked before ANY mutation, so a refusal leaves the database
#    exactly as it was and the script stays "safe to re-run". Aborting later would
#    strand a half-applied upgrade, and because docker-compose gives haf-fyp-setup
#    `restart: on-failure` while every other service waits on
#    `condition: service_completed_successfully`, a mid-script abort would
#    crash-loop the installer and keep the whole stack from ever starting.
#
#    A haf_fyp context owned by any role but haf_fyp_owner is refused here: the block
#    processor cannot attach it, and no UPDATE repairs it. What to DO about it depends on
#    who made it -- an old install as haf_admin (remove it), another application in the
#    HAF registry (do not: that is its context), or something else (find out first) -- so
#    the check and its three messages live in db/install_preconditions.sql, where the
#    integration lane can execute them (#75).
run_sql_file "${SCRIPT_DIR}/../db/install_preconditions.sql"

#    pgvector. db/create_tables.sql declares `interest_vector vector(1024)`, so the type
#    is a hard prerequisite -- but it was documented only in comments here and in the
#    README, and nothing checked it. Without it the install reached step 3 and died on
#    `type "vector" does not exist` AFTER roles and the schema existed, then crash-looped
#    under `restart: on-failure`.
#
#    Checked on to_regtype('vector'), NOT on a pg_extension row: those differ. An
#    extension installed into a schema this connection cannot resolve (not on
#    search_path, or no USAGE) has a pg_extension row and still fails at step 3 -- the
#    exact failure this guard exists to prevent, with a false all-clear in the log.
#    db/create_tables.sql sets no role and no search_path, so the type resolves in the
#    same session, role and search_path as this probe.
#
#    Creates it when this connection is a superuser. An earlier revision refused instead,
#    on the stated grounds that "this runs as haf_admin, which is not a superuser" -- that
#    is false. HAF creates haf_admin AS a superuser, and the sibling app with the same
#    prerequisite relies on it: hivesense's install_app.sh runs `CREATE EXTENSION IF NOT
#    EXISTS vector` and `plpython3u` on this same connection, and plpython3u is untrusted,
#    so there is no non-superuser path to it. Refusing here would have left the operator
#    in the crash loop, reading a message telling them to run, as a superuser, the one
#    statement the connection had just declined to run.
psql_cmd -v ON_ERROR_STOP=on -c "
DO \$\$
DECLARE __schema name;
BEGIN
    IF to_regtype('vector') IS NOT NULL THEN
        RETURN;
    END IF;

    SELECT n.nspname INTO __schema
    FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
    WHERE e.extname = 'vector';

    IF __schema IS NOT NULL THEN
        RAISE EXCEPTION
            'pgvector is installed in schema %, which this connection (%) cannot '
            'resolve: search_path is %. db/create_tables.sql declares '
            'interest_vector vector(1024) unqualified, so the install would still '
            'fail at step 3. Nothing has been modified.',
            __schema, current_user, current_setting('search_path');
    END IF;

    IF current_setting('is_superuser') = 'on' THEN
        BEGIN
            EXECUTE 'CREATE EXTENSION IF NOT EXISTS vector';
        EXCEPTION WHEN duplicate_object OR unique_violation THEN
            -- CREATE EXTENSION IF NOT EXISTS is not concurrency-safe: a HiveSense
            -- install running against the same database at the same moment can win
            -- the race and leave us with a duplicate-key error on
            -- pg_extension_name_index. Either way the extension now exists.
            NULL;
        END;
        RAISE NOTICE 'pgvector was missing; created it. HiveSense creates the same '
                     'extension on the same connection.';
        RETURN;
    END IF;

    RAISE EXCEPTION
        'pgvector is not installed in database %, and % is not a superuser, so this '
        'script cannot create it. haf_fyp stores interest vectors as vector(1024), so '
        'the install would fail at step 3 after creating roles and the schema. In '
        'database %, as a superuser, run:  CREATE EXTENSION IF NOT EXISTS vector;  '
        '-- or install HiveSense, which creates it. Nothing has been modified.',
        current_database(), current_user, current_database();
END
\$\$;"

log "Installing haf_fyp schema..."

# 1. Roles (must run as haf_admin)
run_sql_file "${SCRIPT_DIR}/../db/builtin_roles.sql"

# 2. Schemas, control tables, lifecycle functions, main loop
run_sql_file "${SCRIPT_DIR}/../db/haf_fyp_app.sql"

# 2b. Application version table + singleton machinery (#20). Split out of haf_fyp_app.sql
#     so the postgrest CI lane can load the shipped DDL instead of a copy; see the header
#     of db/app_version.sql. Must follow step 2, which creates the schema.
run_sql_file "${SCRIPT_DIR}/../db/app_version.sql"

# 3. Application tables and composite type
run_sql_file "${SCRIPT_DIR}/../db/create_tables.sql"

# 3b. Feed-events retention + rollup functions (#12) — needs the tables above
run_sql_file "${SCRIPT_DIR}/../db/feed_events_maintenance.sql"

# 3c. Embedding-coverage refresh function (#15) — needs the tables above
run_sql_file "${SCRIPT_DIR}/../db/embedding_coverage_maintenance.sql"

# 3d. Rotation eviction + #9-invariant repair functions (#10) — needs the tables above
run_sql_file "${SCRIPT_DIR}/../db/active_users_maintenance.sql"

# 4. Phase 2: post ingestion
run_sql_file "${SCRIPT_DIR}/../db/process_posts.sql"

# 5. Phase 3: engagement tracking
run_sql_file "${SCRIPT_DIR}/../db/process_engagement.sql"

# 6. Phase 4: community subscription sync
run_sql_file "${SCRIPT_DIR}/../db/process_communities.sql"

# 6b. Phase 8: per-user interaction recording (feeds the ranker's interest vectors)
run_sql_file "${SCRIPT_DIR}/../db/process_interactions.sql"

# 7. Block range dispatcher (loads after all processors are defined)
run_sql_file "${SCRIPT_DIR}/../db/process_block_range.sql"

# 8. Seed data (algorithm versions)
run_sql_file "${SCRIPT_DIR}/../db/seed_data.sql"

# 9. API endpoints (includes score explainer + algorithm registry — Phase 9)
#
# First the base URL the published document advertises (#58). This used to be
# `SET custom.swagger_url` on THIS psql session, which nothing read and nothing could
# have: PostgREST connects later on its own pool. It is persisted in
# haf_fyp_app.app_config (db/haf_fyp_app.sql, step 2) and read by
# haf_fyp_endpoints.root() at request time.
#
# Tell the operator when their value will be ignored. The flag is documented as the way
# to set the base URL, so without this the only way to find out is to read the SQL.
# Keep both patterns identical to the ones in haf_fyp_endpoints.root(): that function
# is the enforcer, this only reports.
#
# [[ =~ ]], not grep: grep matches line by line, and root() matches the whole value. A
# value with a newline in it could pass here on its second line and be refused there.
swagger_shape='^(/($|[^/])|https?://[^/?#@]+/)'
swagger_forbidden='[[:space:][:cntrl:]\\]'
if [ -n "${SWAGGER_URL}" ] && {
    [[ ! "${SWAGGER_URL}" =~ ${swagger_shape} ]] || [[ "${SWAGGER_URL}" =~ ${swagger_forbidden} ]]
}; then
    log "WARNING: --swagger-url='${SWAGGER_URL}' is not an absolute path or an http(s)"
    log "         URL with a path, so the published OpenAPI document will advertise the"
    log "         default base URL '/haf-fyp-api' instead. Pass a path (e.g. '/' for a"
    log "         standalone stack, '/haf-fyp-api' behind a node) to set it."
fi

# -v and :'swagger_url', NOT string interpolation. The old line spliced the value into
# the SQL text, and `psql -c` runs several statements: --swagger-url="x'; DROP ...; --"
# closed the literal and ran the rest as the installing role, and a value with one
# apostrophe in it broke the install with a syntax error far from its cause. psql now
# gets the value out of band and does the quoting.
#
# -f, not -c: psql applies :'var' substitution to a file and to stdin, but hands a -c
# string to the server untouched. The statements are in scripts/set_swagger_base_url.sql
# so that tests/integration/test_swagger_base_url_db.py runs them, not a copy of them.
#
# An empty value is stored as NULL, so a re-install without the flag puts the default
# back rather than keeping whatever an earlier install set.
psql_cmd -v swagger_url="${SWAGGER_URL}" -f "${SCRIPT_DIR}/../scripts/set_swagger_base_url.sql"

run_sql_file "${SCRIPT_DIR}/../endpoints/endpoint_schema.sql"

# 9b. Sync-status reader (#16) — SECURITY DEFINER over hafd.contexts, owned by
#     haf_fyp_owner. Needs the schema (step 2) and the roles (step 1); the context
#     itself is resolved at call time, so this may load before step 10.
run_sql_file "${SCRIPT_DIR}/../db/sync_status.sql"

# 10. Register HAF context (idempotent) — as haf_fyp_owner, NOT as haf_admin.
#     hive.app_create_context stores owner = current_user, and HAF's RLS UPDATE
#     policy on hafd.contexts is
#         sp_applications_update_hive_context
#             WITH CHECK (hive.can_impersonate(CURRENT_USER, owner))
#     can_impersonate only walks role membership *upward*, so a context created by
#     haf_admin can never be advanced by the haf_fyp_owner block processor —
#     GRANT haf_fyp_owner TO haf_admin runs the other way. Every other app on a
#     live HAF node owns its own context row (hafbe_app -> hafbe_owner,
#     haf_stats -> haf_stats_owner); haf_fyp must match.
psql_cmd -c "SET ROLE haf_fyp_owner; SELECT haf_fyp_app.create_context_if_not_exists(); RESET ROLE;"
# 10b. Registry registration (haf#341). Must follow the context creation above
#      and db/haf_fyp_app.sql (step 2), which defines haf_fyp_app.process_blocks:
#      hive.app_register verifies that entry procedure with to_regprocedure().
#      Warns and skips on a HAF without the registry; see db/register_app.sql.
psql_cmd -c "SET ROLE haf_fyp_owner;" -f "${SCRIPT_DIR}/../db/register_app.sql"

# 11. Grant read access on current and future objects to haf_fyp_user
psql_cmd -c "
    ALTER DEFAULT PRIVILEGES FOR ROLE haf_fyp_owner IN SCHEMA haf_fyp_app
        GRANT SELECT ON TABLES TO haf_fyp_user;
    ALTER DEFAULT PRIVILEGES FOR ROLE haf_fyp_owner IN SCHEMA haf_fyp_endpoints
        GRANT EXECUTE ON FUNCTIONS TO haf_fyp_user;
"

# 12. Cross-schema grants (hivesense_app, reptracker_app, btracker_app, hafd).
#     Schemas that don't exist yet are skipped with a NOTICE — re-run
#     scripts/grant_cross_schema.sh after installing each external app.
#
#     The outcome is inspected here, not only in grant_cross_schema.sh. This is the
#     PRIMARY path — docker-compose's haf-fyp-setup runs install_app.sh and never runs
#     grant_cross_schema.sh — and "Self-healing FAILED" is a RAISE WARNING, which does
#     not trip ON_ERROR_STOP. Without this check the installer prints success, every
#     dependent service starts, and self-healing is silently off: the same
#     warning-in-a-scrolling-log failure that cross_schema_grants.sql's own handler
#     comment argues must reach ON_ERROR_STOP. set +e for the reason given in
#     grant_cross_schema.sh: a failing command substitution under set -e kills the
#     script before its output can be replayed.
set +e
_grants_output="$(run_sql_file "${SCRIPT_DIR}/../db/cross_schema_grants.sql" 2>&1)"
_grants_status=$?
set -e
echo "${_grants_output}"

if [ "${_grants_status}" -ne 0 ]; then
    log "ERROR: cross_schema_grants.sql failed (psql exit ${_grants_status}) — see output above."
    exit "${_grants_status}"
fi

if grep -q 'Self-healing FAILED' <<<"${_grants_output}"; then
    log "ERROR: cross-schema self-healing set nothing while targets are installed."
    log "       The credibility signal will silently lose its grants the next time a"
    log "       foreign app is upgraded. Install as haf_admin, or as a member of the"
    log "       foreign owner roles, then re-run."
    exit 1
fi

# 13. Post-install assertion for GET /sync-status (#16). SET ROLE drops superuser,
#     so this runs the endpoint's exact query as the exact role, exercising the
#     real EXECUTE grant, the SECURITY DEFINER hop, the definer's real group
#     membership and HAF's real RLS policy. It is the ONLY place any of that is
#     checked against HAF itself: the install-test lane (#47) runs this script, but
#     over tests/haf_stub.sql, whose policy is a model of HAF's; and the fastapi-tests
#     lane's haf_fyp_user is a superuser for whom every ACL and policy is bypassed.
#     A zero-row result (not an error) is the signature of a missing or
#     non-inheriting membership on the definer, so assert on the count.
psql_cmd -v ON_ERROR_STOP=on -c "
    SET ROLE haf_fyp_user;
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM haf_fyp_app.sync_status()) THEN
            RAISE EXCEPTION
                'haf_fyp_app.sync_status() returned no row: GET /sync-status would '
                'report a missing context. Either the HAF context is absent, or '
                'haf_fyp_owner (the function definer) does not INHERIT '
                'hive_applications_group, so RLS on hafd.contexts hides every row '
                '(see db/builtin_roles.sql).';
        END IF;
    END
    \$\$;
    RESET ROLE;
"

# 14. Post-install assertion that the two clocks agree. The context pointer and
#     app_status.last_processed_block are one clock (see db/haf_fyp_app.sql), and
#     step 10 has just run create_context_if_not_exists(), which seeds or repairs the
#     pointer. This is the only place that repair runs against the REAL hive.* API; the
#     postgrest CI lane does not load db/haf_fyp_app.sql at all, and
#     tests/ci_invariants.sql asserts the same invariant against the stub. Since #65 the
#     function's branches are also driven directly in
#     tests/integration/test_install_context_pointer_db.py, against the stub's context
#     lifecycle -- which is a model of HAF, not HAF.
#
#     A desynced pointer is silent by default: GET /sync-status answers HTTP 200 with
#     {"last_block_num": 0, "last_block_time": null} because the context row exists, and
#     haf_api_node's HTTP probe then reads that as a permanent false DOWN.
psql_cmd -v ON_ERROR_STOP=on -c "
DO \$\$
DECLARE
    __ctx int;
    __app int;
    __rows bigint;
    __claimed_by TEXT;
BEGIN
    SELECT count(*) INTO __rows FROM haf_fyp_app.app_status;
    IF __rows <> 1 THEN
        RAISE EXCEPTION
            'haf_fyp_app.app_status holds % rows, expected exactly 1. The LIMIT 1 readers '
            'have no ORDER BY, so get_health and the ranker sync gate would pick an '
            'arbitrary row. Re-running db/haf_fyp_app.sql de-duplicates them.', __rows;
    END IF;

    SELECT c.current_block_num INTO __ctx FROM hafd.contexts AS c WHERE c.name = 'haf_fyp';
    SELECT max(s.last_processed_block) INTO __app FROM haf_fyp_app.app_status AS s;

    IF __ctx IS NULL THEN
        RAISE EXCEPTION 'no hafd.contexts row named haf_fyp after install';
    END IF;

    -- A context another application owns is NOT ours to repair (#65), so the assertion
    -- below would be asserting something this install deliberately did not do. Without
    -- this branch the new guard in create_context_if_not_exists() would turn a warning
    -- into a failed install whenever the context sits at 0 with app_status above it --
    -- and a failed install is haf-fyp-setup restarting forever with haf_fyp's other
    -- services waiting on it.
    __claimed_by := haf_fyp_app.context_claimed_by_other_app('haf_fyp');

    IF __claimed_by IS NOT NULL THEN
        RAISE WARNING
            'HAF context haf_fyp (block %) belongs to application %, so this install did '
            'not seed or repair the pointer and the two clocks are not expected to agree '
            '(app_status: %). haf_fyp is installed but will not process blocks.',
            __ctx, __claimed_by, __app;
    ELSIF __app > 0 AND __ctx = 0 THEN
        RAISE EXCEPTION
            'HAF context is at block 0 while app_status reports % processed, and '
            'create_context_if_not_exists() did not repair it. GET /sync-status would '
            'report block 0 with a null timestamp on a synced node and the processor '
            'would replay from genesis.', __app;
    END IF;

    -- Skipped when another application owns the context: the clocks are then not
    -- expected to agree, the line above has already said so, and a second warning saying
    -- the same thing in different words is how an operator learns to skim them.
    IF __claimed_by IS NULL AND abs(__ctx - __app) > 10000 THEN
        RAISE WARNING
            'HAF context (%) and app_status (%) differ by more than one 10000-block '
            'batch. If the block processor is running this is an in-flight batch and '
            'will settle; if it is not, the two clocks have drifted.', __ctx, __app;
    END IF;
END
\$\$;"

# 15. Application version (#20). GIT_HASH arrives from Dockerfile.sql's ARG/ENV pair,
#     which docker-compose bakes at build time, or straight from the environment on a
#     source-tree install. Unset means nobody told us which commit this is, so we record
#     the placeholder EXPLICITLY rather than leave whatever is already there.
#
#     That unconditional rewrite is the point, not a detail. app_version must describe the
#     install that just ran. An earlier version of this step wrote nothing when GIT_HASH
#     was unset, on the reasoning that db/app_version.sql's seed would still be in place --
#     but that seed is guarded (ON CONFLICT DO NOTHING) and cannot refresh a table that
#     already holds a row. Re-installing a node that had been installed WITH a hash
#     therefore left the PREVIOUS commit's hash in the table, step 16 passed it, and GET
#     /version confidently named a commit the node was no longer running. That is strictly
#     worse than the null in #20: a null is obviously broken, a plausible-looking short SHA
#     is not. A wrong hash is undetectable; a placeholder explains itself.
#
#     RUNS LAST, after every assertion above, because a version row is a claim about what
#     is installed and must not be written until the install has actually succeeded. Steps
#     13 and 14 can still RAISE EXCEPTION; stamping the new commit ahead of them would
#     leave haf-fyp-setup crash-looping under restart: on-failure while /version reported a
#     deploy that never finished -- the same undetectable-wrong-hash failure in a different
#     costume. It is also the cheapest step to fail: by here the node is fully installed, so
#     a redeploy that trips only on this leaves a serving stack alone.
#
#     SET LOCAL ROLE is correct here because ownership is uniform from step 3 onward:
#     db/create_tables.sql reassigns every haf_fyp_app table to haf_fyp_owner. Run next to
#     the table at step 2b instead, on a FRESH install, and the table is still owned by
#     whoever connected, so SET ROLE would fail. Here it holds under both installer
#     identities -- haf_admin, and the haf_fyp_owner default from scripts/common.sh.
#
#     DELETE + INSERT + UPDATE wrapped in one transaction, not haf_stats' TRUNCATE; INSERT.
#     psql autocommits per statement, so top-level statements would be separate
#     transactions with a window in between where the table is empty and a live node
#     answers /version with null -- permanently, if a later statement failed. DELETE also
#     needs only the DELETE privilege where TRUNCATE needs ownership.
#
#     The INSERT takes VALUES (DEFAULT), so the placeholder string has exactly one
#     definition -- the column default in db/app_version.sql -- and cannot drift between
#     "installed without a hash" and "install died before this step", the two states it
#     exists to tell apart. The UPDATE then overwrites it only when a hash was supplied,
#     which keeps both cases on one code path.
#
#     The statement lives in scripts/set_version.sql because the postgrest CI lane runs it
#     too; keeping one file means the lane exercises this step rather than a copy of it.
#
#     -f, not -c: psql performs variable substitution only when reading a file or stdin.
#     Passed to -c the string goes to the server verbatim and dies with
#     `syntax error at or near ":"`. run_sql_file cannot be used either -- it forwards no
#     -v (scripts/common.sh) -- so this reaches for psql_cmd directly, as step 9 does.
#     :'git_hash' is psql's quote-and-escape substitution, so a hash carrying a quote
#     cannot break out of the literal into SQL that runs with the installer's rights.
#
#     Ordering can still leave a stale hash in one direction nothing can fix: an in-place
#     re-install that aborts at step 12, 13 or 14 has already applied the new SQL in steps
#     1-11 while this step never runs, so /version keeps naming the previous commit on a
#     partially upgraded schema. Running last is the least-bad choice, not a guarantee.
if [ -n "${GIT_HASH:-}" ]; then
    log "Recording application version: ${GIT_HASH}"
else
    log "WARNING: GIT_HASH is unset — recording the db/app_version.sql placeholder."
    log "         GET /version will not name a commit. GIT_HASH is baked into the image"
    log "         at build time, so exporting it and re-running is NOT enough -- a plain"
    log "         'up' reuses the existing image. Rebuild, with the block processor"
    log "         stopped -- it holds HAF's lock on haf_fyp and the install skips (#67):"
    log "         see README, 'Re-installing while the stack runs'."
fi

psql_cmd -v git_hash="${GIT_HASH:-}" -f "${SCRIPT_DIR}/../scripts/set_version.sql"

# 16. Assert the version row is a usable singleton. This is the only place the #20 fix
#     is checked against a real node (the install-test lane, #47, runs it over the HAF
#     stub). It asserts a usable row, not a real commit, so the GIT_HASH-unset path
#     above still passes.
psql_cmd -c "
DO \$\$
DECLARE
    __rows bigint;
    __hash text;
BEGIN
    SELECT count(*) INTO __rows FROM haf_fyp_app.app_version;
    IF __rows <> 1 THEN
        RAISE EXCEPTION
            'haf_fyp_app.app_version holds % rows, expected exactly 1. get_version() and '
            'get_health() take LIMIT 1 with no ORDER BY, so they would return an arbitrary '
            'hash. Re-run db/app_version.sql to de-duplicate, then re-run this installer so '
            'step 15 rewrites the survivor -- the collapse keeps an arbitrary row, not the '
            'newest one.', __rows;
    END IF;

    SELECT v.git_hash INTO __hash FROM haf_fyp_app.app_version AS v;
    IF __hash IS NULL OR btrim(__hash) = '' THEN
        RAISE EXCEPTION
            'haf_fyp_app.app_version.git_hash is empty; GET /version would answer with an '
            'empty string, as unreadable as the null in #20.';
    END IF;
END
\$\$;"

log "Installation complete."
log "If any cross-schema NOTICEs appeared above, install the missing apps"
log "then run: scripts/grant_cross_schema.sh"
