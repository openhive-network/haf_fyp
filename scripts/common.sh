#!/bin/bash
set -euo pipefail

# Shared variables and helpers for all haf_fyp scripts.

: "${POSTGRES_HOST:=haf}"
: "${POSTGRES_PORT:=5432}"
: "${POSTGRES_DB:=haf_block_log}"
: "${POSTGRES_USER:=haf_fyp_owner}"

HAF_FYP_SCHEMA="haf_fyp_app"

log() {
    echo "[haf_fyp $(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"
}

wait_for_postgres() {
    log "Waiting for PostgreSQL at ${POSTGRES_HOST}:${POSTGRES_PORT}..."
    until pg_isready -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -q; do
        sleep 2
    done
    log "PostgreSQL is ready."
}

psql_cmd() {
    psql \
        -h "${POSTGRES_HOST}" \
        -p "${POSTGRES_PORT}" \
        -U "${POSTGRES_USER}" \
        -d "${POSTGRES_DB}" \
        -v ON_ERROR_STOP=on \
        "$@"
}

run_sql_file() {
    local file="$1"
    log "Executing ${file}..."
    psql_cmd -f "${file}"
}
