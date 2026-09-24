#!/bin/sh
# Resolve every image this repo's compose files PULL, and fail when one does not exist (#63).
#
# Nothing else notices. compose-validate runs `docker compose config`, which parses YAML and
# never contacts a registry -- and deliberately passes placeholder values, because a lane
# that cannot pull cannot tell a good reference from a dead one. docker-compose-ci.yml runs
# plain PostgreSQL with pgvector, so no CI lane pulls HAF at all. The guards !72 added in
# tests/unit/test_compose_contract.py are offline by design: they keep the reference in the
# shape that resolved when it was last run, and none of them can say whether the tag is
# still in the registry. That combination is how #56 lived -- the stack asked for
# ${HAF_REGISTRY}/haf/instance:${HAF_VERSION}, nothing was ever published there, and every
# pipeline stayed green until an operator hit it on a cold cache.
#
# POSIX sh, not bash: the job's image is docker:24, which is Alpine and whose /bin/sh is
# BusyBox ash. shell-lint reads this directory, and the shebang is what tells it so.
#
# NO DAEMON AND NO LOGIN, both deliberate and both verified against the real registries:
#
#   `docker buildx imagetools inspect` is client-side -- it talks to the registry itself. In
#   a docker:24 container with no socket mounted, `docker info` fails while this succeeds,
#   so the job needs no dind service.
#
#   Anonymous, because these are public repositories and that is how an operator pulls them.
#   Authenticating would hide exactly the failure this exists to catch: a repository that has
#   become unreadable without our credentials still resolves for CI.
set -eu

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH='' cd -- "${SCRIPT_DIR}/.." && pwd)"

# docker/.env, not .env.local and not the environment: the point is to resolve what this
# repository SHIPS. An operator's override is their business; a dead pin in a tracked file
# is everybody's.
ENV_FILE="${HAF_FYP_ENV_FILE:-${REPO_ROOT}/docker/.env}"

# Both files, because both carry pins that rot. docker-compose-ci.yml is what the test lanes
# themselves run on (pgvector, postgres, postgrest, python), so a tag disappearing there
# breaks CI rather than an operator.
COMPOSE_FILES="${HAF_FYP_COMPOSE_FILES:-${REPO_ROOT}/docker/docker-compose.yml ${REPO_ROOT}/docker/docker-compose-ci.yml}"

# STDERR, all of it. An earlier revision logged to stdout, and because the extraction below
# runs in a command substitution, its own error message was captured AS THE IMAGE LIST --
# the job then reported seven unresolved images with names like "such" and "file:" and
# exited 1 for entirely the wrong reason. A diagnostic that can be mistaken for data is
# worse than none.
log() { printf '[check-pinned-images] %s\n' "$*" >&2; }

# Checked BEFORE the extraction, not inside it. `pulled_images` ends in a pipeline, so its
# left-hand side runs in a subshell and a `return 1` from there sets the status of nothing
# -- the function still exits with `sort`'s success. This is that bug's fix, and the reason
# it is a separate loop.
for _file in ${COMPOSE_FILES}; do
    if [ ! -f "${_file}" ]; then
        log "ERROR: no such compose file: ${_file}"
        log "       Nothing was checked, which is NOT a pass."
        exit 1
    fi
done
if [ ! -f "${ENV_FILE}" ]; then
    log "ERROR: no such env file: ${ENV_FILE}"
    log "       Without it every \${VAR} resolves empty and the references are meaningless."
    exit 1
fi

# Services that PULL -- `config --images` cannot be used here. It prints a synthesised name
# for every build-only service (haf_fyp-haf-fyp-setup, ...), which exists in no registry, so
# the check would report five failures on a perfectly healthy stack and be switched off
# within a week. A service that carries `image:` AND `build:` (the rewriter) is kept: compose
# pulls it and falls back to building only when the pull fails, which is the very thing worth
# knowing about.
pulled_images() {
    for _f in ${COMPOSE_FILES}; do
        docker compose --env-file "${ENV_FILE}" -f "${_f}" config --format json \
            | jq -r '.services | to_entries[] | select(.value.image != null) | .value.image'
    done | sort -u
}

if ! _refs="$(pulled_images)"; then
    log "ERROR: could not read the compose files. Nothing was checked, which is NOT a pass."
    exit 1
fi

if [ -z "${_refs}" ]; then
    # Never silently green. If the extraction stops finding images -- a compose schema
    # change, a jq filter that no longer matches -- this job would otherwise pass while
    # checking nothing at all, which is the shape of the problem #63 is about.
    log "ERROR: no images were extracted from ${COMPOSE_FILES}."
    log "       A check that examines nothing must not report success."
    exit 1
fi

_failed=0
_checked=0
_errfile="$(mktemp)"
_manifest="$(mktemp)"
trap 'rm -f "${_errfile}" "${_manifest}"' EXIT

for _ref in ${_refs}; do
    _checked=$((_checked + 1))
    if ! docker buildx imagetools inspect --format '{{json .Manifest}}' "${_ref}" \
            >"${_manifest}" 2>"${_errfile}"; then
        _failed=$((_failed + 1))
        log "UNRESOLVED ${_ref}"
        sed 's/^/    /' "${_errfile}" >&2
        continue
    fi

    # `{{json .Manifest}}`, and then a shape check, because buildx does NOT reject a
    # template it cannot resolve: `--format '{{.Manifest.Digest}}'` exits 0 and prints the
    # DEFAULT human block instead (Name:/MediaType:/Digest:), so a typo here would have this
    # job reporting three lines of prose per image and passing forever.
    _digest="$(jq -r '.digest // empty' "${_manifest}" 2>/dev/null || true)"
    case "${_digest}" in
        sha256:*) ;;
        *)
            _failed=$((_failed + 1))
            log "UNREADABLE ${_ref}: no sha256 digest in buildx's output"
            head -c 300 "${_manifest}" | sed 's/^/    /' >&2
            continue
            ;;
    esac

    # The DIGEST OF THE MANIFEST LIST, which is what `docker pull` records and what a
    # re-point changes. `docker manifest inspect --verbose` gives PER-PLATFORM digests
    # instead -- for postgrest/postgrest:v12.2.3 that is sha256:bd918a7d..., while the list
    # is sha256:729bf65c... -- so reporting that one would make a re-pointed tag look
    # unchanged, which is the failure this is supposed to surface. `stable` was re-pointed
    # upstream without its created_at moving; a digest in the log makes that visible as a
    # change between runs instead of an unbroken row of green.
    printf '%s %s\n' "${_digest}" "${_ref}"
done

log "checked ${_checked} image(s), ${_failed} unresolved"
if [ "${_failed}" -ne 0 ]; then
    log "A pinned image no longer resolves. A fresh \`docker compose up\` dies on the pull;"
    log "a host with the image cached keeps working until it pulls again. Fix the pin in"
    log "${ENV_FILE} (or the compose file), then re-run."
    exit 1
fi
