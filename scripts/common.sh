#!/bin/bash
set -euo pipefail

# Shared variables and helpers for all haf_fyp scripts.

: "${POSTGRES_HOST:=haf}"
: "${POSTGRES_PORT:=5432}"
: "${POSTGRES_DB:=haf_block_log}"
: "${POSTGRES_USER:=haf_fyp_owner}"

# A full libpq connection URI or keyword/value conninfo. When set it REPLACES the four
# settings above -- it is not merged with them, because a URI that is half-honoured is
# worse than one that is refused: the script would act on one database while reporting
# another. Everything below routes through pg_conn_args/pg_target, so there is exactly
# one connection path and every message describes the connection actually made.
#
# haf_api_node's hivemind.yaml passes
#   --postgres-url=postgresql://haf_admin@haf/haf_block_log
# to its uninstall service, and all three of balance_tracker's scripts take the flag.
: "${POSTGRES_URL:=}"

# application_name travels via PGAPPNAME, NOT as a ?application_name= query parameter.
# A caller-supplied URI would silently drop a query parameter we appended, and the
# uninstall session guard excludes the healthcheck probe by that exact literal -- so the
# probe would start counting itself and the teardown would refuse at random. libpq reads
# PGAPPNAME whichever way the connection is specified, and an explicit application_name
# inside a supplied URI still wins, which is the right precedence.
: "${PGAPPNAME:=}"
export PGAPPNAME

# The connection flags, as an argv array. One definition, used by psql AND pg_isready,
# so the two can never disagree about where they are looking.
pg_conn_args() {
    if [ -n "${POSTGRES_URL}" ]; then
        printf '%s\n' -d "${POSTGRES_URL}"
    else
        printf '%s\n' -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" \
                       -U "${POSTGRES_USER}" -d "${POSTGRES_DB}"
    fi
}

# What to PRINT for the connection. Never the raw URL: a libpq conninfo can carry a
# password, and these scripts run in CI and under `docker compose logs`.
#
# Redacts POSITIVELY -- it blanks the whole userinfo section and every password= key,
# rather than trying to match the shape a secret happens to take. The previous attempt
# pattern-matched `://user:secret@host` and leaked both of the other two forms libpq
# accepts, verified inside the real Alpine image:
#     postgresql://u@h/db?password=SECRET      and      host=h password=SECRET dbname=x
redacted_target() {
    if [ -z "${POSTGRES_URL}" ]; then
        printf '%s' "${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"
        return
    fi
    # URI form: drop the userinfo AND the whole query string, leaving scheme://host/db.
    # Nothing secret survives that, which is the point -- an earlier version tried to
    # match the SHAPE of a secret and leaked two of the three forms libpq accepts,
    # verified inside the real Alpine image: ?password= and keyword/value conninfo.
    # Keyword/value form: blank every password= key.
    printf '%s' "${POSTGRES_URL}" \
        | sed -E 's#://[^/@]*@#://#; s#\?.*$##; s#(^|[[:space:]])password=[^[:space:]]*#\1password=REDACTED#g'
}
export HAF_FYP_SCHEMA="haf_fyp_app"

log() {
    echo "[haf_fyp $(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"
}

wait_for_postgres() {
    local -a args
    mapfile -t args < <(pg_conn_args)

    # Validate ONCE, loudly, before entering the unbounded loop. pg_isready returns 2
    # (PQPING_NO_ATTEMPT) for a conninfo it cannot even parse, and -q hides the reason --
    # so a typo'd URL is indistinguishable from "still starting" and the `until` below
    # never terminates. A permanent hang is a worse failure than a clear error.
    if ! pg_isready "${args[@]}" >/dev/null 2>&1; then
        local rc=$?
        if [ "$rc" -eq 2 ]; then
            log "ERROR: cannot parse the connection settings for $(redacted_target)."
            pg_isready "${args[@]}" 2>&1 | sed 's/^/       /' || true
            exit 2
        fi
    fi

    log "Waiting for PostgreSQL at $(redacted_target)..."
    until pg_isready "${args[@]}" -q; do
        sleep 2
    done
    log "PostgreSQL is ready."
}

psql_cmd() {
    local -a args
    mapfile -t args < <(pg_conn_args)
    # The discrete flags are NOT also passed in the URI case: a trailing -U overrides the
    # URI's own user, which is the opposite of "the URL wins".
    psql "${args[@]}" -v ON_ERROR_STOP=on "$@"
}

# The identity the DATABASE reports, for messages that would otherwise interpolate
# ${POSTGRES_USER} -- what was REQUESTED. The two differ whenever a URI, PGUSER,
# PGSERVICE or .pgpass redirects the connection, and a message naming the wrong one sends
# the operator to change the wrong thing.
connected_identity() {
    psql_cmd -Atq -c "SELECT current_user || '@' || current_database()" 2>/dev/null || echo ""
}

run_sql_file() {
    local file="$1"
    log "Executing ${file}..."
    psql_cmd -f "${file}"
}
