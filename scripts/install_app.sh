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

log "Installing haf_fyp schema..."

# 1. Roles (must run as haf_admin)
run_sql_file "${SCRIPT_DIR}/../db/builtin_roles.sql"

# 2. Schemas, control tables, lifecycle functions, main loop
run_sql_file "${SCRIPT_DIR}/../db/haf_fyp_app.sql"

# 3. Application tables and composite type
run_sql_file "${SCRIPT_DIR}/../db/create_tables.sql"

# 4. Phase 2: post ingestion
run_sql_file "${SCRIPT_DIR}/../db/process_posts.sql"

# 5. Phase 3: engagement tracking
run_sql_file "${SCRIPT_DIR}/../db/process_engagement.sql"

# 6. Phase 4: community subscription sync
run_sql_file "${SCRIPT_DIR}/../db/process_communities.sql"

# 7. Block range dispatcher (loads after all processors are defined)
run_sql_file "${SCRIPT_DIR}/../db/process_block_range.sql"

# 8. Seed data (algorithm versions)
run_sql_file "${SCRIPT_DIR}/../db/seed_data.sql"

# 9. API endpoints (includes score explainer + algorithm registry — Phase 9)
psql_cmd -c "SET custom.swagger_url = '${SWAGGER_URL}';" -f "${SCRIPT_DIR}/../endpoints/endpoint_schema.sql"

# 10. Register HAF context (idempotent) — must run as haf_fyp_owner so that
#     hafd.contexts.owner = haf_fyp_owner, satisfying the RLS policies in HAF.
psql_cmd -c "SET ROLE haf_fyp_owner; SELECT haf_fyp_app.create_context_if_not_exists(); RESET ROLE;"

# 10a. Reassign ownership of HAF-created schema objects to haf_fyp_owner.
#      hive.app_create_context() runs as SECURITY DEFINER (haf_admin), so views
#      and the internal tracking table are created owned by haf_admin. The process
#      container connects as haf_fyp_owner and needs to own them for CREATE OR
#      REPLACE VIEW to succeed during context attach.
psql_cmd -c "
DO \$\$
DECLARE obj RECORD;
BEGIN
  FOR obj IN
    SELECT c.relname, c.relkind::text AS kind
    FROM pg_class c
    JOIN pg_namespace n ON c.relnamespace = n.oid
    WHERE n.nspname = 'haf_fyp_app'
      AND c.relkind IN ('r', 'v', 'S')
      AND c.relowner != (SELECT oid FROM pg_roles WHERE rolname = 'haf_fyp_owner')
  LOOP
    IF obj.kind = 'v' THEN
      EXECUTE format('ALTER VIEW haf_fyp_app.%I OWNER TO haf_fyp_owner', obj.relname);
    ELSIF obj.kind = 'r' THEN
      EXECUTE format('ALTER TABLE haf_fyp_app.%I OWNER TO haf_fyp_owner', obj.relname);
    ELSE
      EXECUTE format('ALTER SEQUENCE haf_fyp_app.%I OWNER TO haf_fyp_owner', obj.relname);
    END IF;
  END LOOP;
END
\$\$;"

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

log "Installation complete."
log "If any cross-schema NOTICEs appeared above, install the missing apps"
log "then run: scripts/grant_cross_schema.sh"
