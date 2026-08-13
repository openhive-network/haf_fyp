#!/bin/bash
# One-time schema installation. Run once against a live HAF database.
# Safe to re-run: all DDL uses IF NOT EXISTS / ON CONFLICT DO NOTHING.
#
# PREREQUISITE: pgvector must be installed on the PostgreSQL server:
#   Run as superuser: CREATE EXTENSION IF NOT EXISTS vector;
#
# OPTIONS:
#   --swagger-url=URL   Public hostname for Swagger UI example URLs (default: localhost)

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

SWAGGER_URL="localhost"

for arg in "$@"; do
    case "$arg" in
        --swagger-url=*) SWAGGER_URL="${arg#*=}" ;;
    esac
done

wait_for_postgres

# 0. PRECONDITIONS — checked before ANY mutation, so a refusal leaves the database
#    exactly as it was and the script stays "safe to re-run". Aborting later would
#    strand a half-applied upgrade, and because docker-compose gives haf-fyp-setup
#    `restart: on-failure` while every other service waits on
#    `condition: service_completed_successfully`, a mid-script abort would
#    crash-loop the installer and keep the whole stack from ever starting.
#
#    An earlier install registered the HAF context as haf_admin (old step 10 ran
#    as haf_admin, and hive.app_create_context stores current_user as the owner of
#    the hafd.contexts row, the hafd.contexts_attachment row, the context table and
#    all nine context views). That is NOT repairable with an UPDATE: rewriting only
#    hafd.contexts.owner moves the failure into app_context_attach
#    ("new row violates row-level security policy for table contexts_attachment"),
#    and fixing that column too only reaches "must be owner of view blocks_view".
#    Fail with the one command that does fix it. Any host in this state has never
#    processed a block — under the previous NOLOGIN roles none of the runtime
#    containers could connect — so dropping the context is safe.
psql_cmd -v ON_ERROR_STOP=on -c "
DO \$\$
DECLARE __owner name;
BEGIN
    IF to_regclass('hafd.contexts') IS NULL THEN
        RETURN;  -- not a HAF database yet; later steps report that properly
    END IF;
    SELECT owner INTO __owner FROM hafd.contexts WHERE name = 'haf_fyp';
    IF __owner IS NOT NULL AND __owner <> 'haf_fyp_owner' THEN
        RAISE EXCEPTION
            'haf_fyp context is owned by % — an earlier install registered it as '
            'haf_admin. Its attachment row, context table and context views are '
            'owned by that role too, so the haf_fyp_owner block processor cannot '
            'attach it and an UPDATE of hafd.contexts.owner will not help. Run '
            'SELECT hive.app_remove_context(''haf_fyp''); as haf_admin, then re-run '
            'this script. Nothing has been modified.', __owner;
    END IF;
END
\$\$;"

log "Installing haf_fyp schema..."

# 1. Roles (must run as haf_admin)
run_sql_file "${SCRIPT_DIR}/../db/builtin_roles.sql"

# 2. Schemas, control tables, lifecycle functions, main loop
run_sql_file "${SCRIPT_DIR}/../db/haf_fyp_app.sql"

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
psql_cmd -c "SET custom.swagger_url = '${SWAGGER_URL}';" -f "${SCRIPT_DIR}/../endpoints/endpoint_schema.sql"

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
run_sql_file "${SCRIPT_DIR}/../db/cross_schema_grants.sql"

# 13. Post-install assertion for GET /sync-status (#16). SET ROLE drops superuser,
#     so this runs the endpoint's exact query as the exact role, exercising the
#     real EXECUTE grant, the SECURITY DEFINER hop, the definer's real group
#     membership and HAF's real RLS policy. It is the ONLY place any of that is
#     checked: no CI lane runs this script, and the fastapi-tests lane's
#     haf_fyp_user is a superuser for whom every ACL and policy is bypassed.
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

log "Installation complete."
log "If any cross-schema NOTICEs appeared above, install the missing apps"
log "then run: scripts/grant_cross_schema.sh"
