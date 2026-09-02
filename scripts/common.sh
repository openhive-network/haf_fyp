#!/bin/bash
set -euo pipefail

# Shared variables and helpers for all haf_fyp scripts.

: "${POSTGRES_HOST:=haf}"
: "${POSTGRES_PORT:=5432}"
: "${POSTGRES_DB:=haf_block_log}"
: "${POSTGRES_USER:=haf_fyp_owner}"
# A full libpq connection URI, which OVERRIDES the four settings above when set.
# Upstream's contract, not an invention: balance_tracker's install_app.sh builds
# POSTGRES_ACCESS=${POSTGRES_URL:-"postgresql://$POSTGRES_USER@$POSTGRES_HOST:..."} and
# hands it to psql, and haf_api_node's hivemind.yaml passes
# --postgres-url=postgresql://haf_admin@haf/haf_block_log to its uninstall service. A
# module file copied from either used to die on "is not a valid option".
: "${POSTGRES_URL:=}"

export HAF_FYP_SCHEMA="haf_fyp_app"

log() {
    echo "[haf_fyp $(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"
}

# Never log POSTGRES_URL raw: a libpq URI can carry a password, and these scripts run
# in CI and under `docker compose logs`. Strips exactly the userinfo password.
redacted_url() {
    printf '%s' "$1" | sed -E 's#(://[^:/@]*):[^@/]*@#\1:***@#'
}

wait_for_postgres() {
    if [ -n "${POSTGRES_URL}" ]; then
        log "Waiting for PostgreSQL at $(redacted_url "${POSTGRES_URL}")..."
        until pg_isready -d "${POSTGRES_URL}" -q; do
            sleep 2
        done
    else
        log "Waiting for PostgreSQL at ${POSTGRES_HOST}:${POSTGRES_PORT}..."
        until pg_isready -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -q; do
            sleep 2
        done
    fi
    log "PostgreSQL is ready."
}

psql_cmd() {
    # -d takes a full conninfo/URI. The discrete flags are NOT also passed in that
    # branch: a -U alongside a URI silently overrides the URI's own user, which is the
    # opposite of "the URL wins".
    if [ -n "${POSTGRES_URL}" ]; then
        psql -d "${POSTGRES_URL}" -v ON_ERROR_STOP=on "$@"
    else
        psql \
            -h "${POSTGRES_HOST}" \
            -p "${POSTGRES_PORT}" \
            -U "${POSTGRES_USER}" \
            -d "${POSTGRES_DB}" \
            -v ON_ERROR_STOP=on \
            "$@"
    fi
}

run_sql_file() {
    local file="$1"
    log "Executing ${file}..."
    psql_cmd -f "${file}"
}
