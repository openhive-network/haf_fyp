#!/bin/bash
# One-time schema installation. Run once against a live HAF database.
# Safe to re-run: all DDL uses IF NOT EXISTS / ON CONFLICT DO NOTHING.
#
# PREREQUISITE: pgvector must be installed on the PostgreSQL server:
#   Run as superuser: CREATE EXTENSION IF NOT EXISTS vector;

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

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

# 7. Seed data (algorithm versions)
run_sql_file "${SCRIPT_DIR}/../db/seed_data.sql"

# 8. API endpoints
run_sql_file "${SCRIPT_DIR}/../endpoints/endpoint_schema.sql"

# 9. Register HAF context (idempotent)
psql_cmd -c "SELECT haf_fyp_app.create_context_if_not_exists();"

# 10. Grant read access on current and future objects to haf_fyp_user
psql_cmd -c "
    ALTER DEFAULT PRIVILEGES FOR ROLE haf_fyp_owner IN SCHEMA haf_fyp_app
        GRANT SELECT ON TABLES TO haf_fyp_user;
    ALTER DEFAULT PRIVILEGES FOR ROLE haf_fyp_owner IN SCHEMA haf_fyp_endpoints
        GRANT EXECUTE ON FUNCTIONS TO haf_fyp_user;
"

log "Installation complete."
