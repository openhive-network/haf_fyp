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

# 3b. Feed-events retention + rollup functions (#12) — needs the tables above
run_sql_file "${SCRIPT_DIR}/../db/feed_events_maintenance.sql"

# 3c. Embedding-coverage refresh function (#15) — needs the tables above
run_sql_file "${SCRIPT_DIR}/../db/embedding_coverage_maintenance.sql"

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

# 10. Register HAF context (idempotent)
psql_cmd -c "SELECT haf_fyp_app.create_context_if_not_exists();"

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
