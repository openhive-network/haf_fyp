#!/bin/bash
set -euo pipefail

# Shared variables and helpers for all haf_fyp scripts.

: "${POSTGRES_HOST:=haf}"
: "${POSTGRES_PORT:=5432}"
: "${POSTGRES_DB:=haf_block_log}"
: "${POSTGRES_USER:=haf_fyp_owner}"
export HAF_FYP_SCHEMA="haf_fyp_app"

log() {
    echo "[haf_fyp $(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"
}

# A connection flag given an EMPTY value does not fall back to this script's default and
# does not error -- libpq substitutes its own. Measured on PostgreSQL 17:
#
#     pg_isready -h ""   ->  :5432 - accepting connections      (the LOCAL SOCKET)
#     pg_isready -p ""   ->  127.0.0.1: - accepting connections (the default port)
#     pg_isready -U ""   ->  accepting connections              (the OS user)
#
# That makes `uninstall_app --force --host=${HAF_HOST}` with HAF_HOST unset connect
# somewhere else entirely and drop haf_fyp_app and haf_fyp_endpoints there. --force
# skips the type-the-database-name gate, and --force is the form docker-compose.yml
# ships, so nothing downstream catches it. Refusing the empty value is the only guard.
#
# Whitespace-only is refused as well. It happens to fail loudly on --host= and --port=
# (no response), but --user= accepts it, and it is never intentional on any of them.
require_flag_value() {
    local arg="$1"
    local flag="${arg%%=*}"
    local value="${arg#*=}"

    case "${value}" in
        "")
            log "ERROR: ${flag}= was given an empty value."
            log "       An empty value is NOT ignored -- libpq substitutes its own"
            log "       default: the local socket for --host, 5432 for --port, the"
            log "       OS user for --user. This is almost always an unset variable"
            log "       in a module file or runbook; fix the variable rather than"
            log "       removing the flag."
            exit 2
            ;;
    esac

    # A newline splits one value into what reads as two settings, and every message that
    # echoes it prints only the first line -- so the log names one target while libpq is
    # handed another. Both #40 and its sibling asked for this explicitly.
    case "${value}" in
        *"
"*)
            log "ERROR: ${flag}= was given a value containing a newline."
            log "       Refused: messages that echo it would show only its first line,"
            log "       so the log would name a different target than the connection."
            exit 2
            ;;
    esac

    case "${value}" in
        *[![:space:]]*)
            : # at least one non-whitespace character -- fine
            ;;
        *)
            log "ERROR: ${flag}= was given a whitespace-only value."
            log "       Refused for the same reason as an empty value: it cannot be"
            log "       what the caller meant, and --user= accepts it silently."
            exit 2
            ;;
    esac
}

# The same values reach these variables through the ENVIRONMENT, where the flag guard
# never runs. Empty is already handled: the `:=` above is the COLON form, which
# substitutes on unset OR null, so POSTGRES_HOST="" becomes haf. Whitespace-only is not.
#
# NOTE this covers the shell only. haf_fyp/api.py and haf_fyp/ranker_daemon.py read the
# same variables through os.environ.get(k, default), which returns the default only when
# the key is ABSENT -- see haf_fyp/dbenv.py, which restores these semantics there.
for _var in POSTGRES_HOST POSTGRES_PORT POSTGRES_DB POSTGRES_USER; do
    case "${!_var}" in
        *[![:space:]]*) : ;;
        *)  log "ERROR: ${_var} is set to a whitespace-only value."
            log "       Refused for the same reason as the equivalent flag."
            exit 2
            ;;
    esac
done
unset _var

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
