#!/bin/sh
# Resolve every container image this repository pins, and fail when one does not exist (#63).
#
# Nothing else notices. compose-validate runs `docker compose config`, which parses YAML and
# never contacts a registry -- and deliberately passes placeholder values, because a lane
# that cannot pull cannot tell a good reference from a dead one. docker-compose-ci.yml runs
# plain PostgreSQL with pgvector, so no CI lane pulls HAF at all. The guards !72 added in
# tests/unit/test_compose_contract.py are offline by design: they keep the reference in the
# shape that resolved when it was last run, and none of them can say whether the tag is still
# in the registry. That combination is how #56 lived -- the stack asked for
# ${HAF_REGISTRY}/haf/instance:${HAF_VERSION}, nothing was ever published there, and every
# pipeline stayed green until an operator hit it on a cold cache.
#
# POSIX sh, not bash: the job's image is docker:24, which is Alpine and whose /bin/sh is
# BusyBox ash. shell-lint reads this directory, and the shebang is what tells it so.
#
# NO DAEMON AND NO LOGIN, both verified in that exact image rather than assumed. `docker
# buildx imagetools inspect` and `docker compose config` are client-side: with no socket
# mounted `docker info` fails while both of those succeed, so the job needs no dind service.
# Anonymous, because these are public repositories and that is how an operator pulls them --
# authenticating would hide exactly the failure this exists to catch, a repository that has
# become unreadable without our credentials.
#
# THE ONE THING IT MUST NEVER DO IS PASS WHILE CHECKING LESS THAN IT CLAIMS. Every route to a
# short list is a hard failure: a compose file that will not parse, a compose file that
# yields no images, a Dockerfile with no readable FROM, an empty final list. Three reviews of
# the first version of this script found the same hole from three directions -- with two
# compose files, a failure on either was invisible, because `docker compose` sat on the LEFT
# of a pipeline and the function returned `sort`'s status. It reported "checked 4 image(s), 0
# unresolved" and exited 0 with the HAF pin never contacted.
set -euf

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH='' cd -- "${SCRIPT_DIR}/.." && pwd)"

# docker/.env, not .env.local: the point is to resolve what this repository SHIPS. An
# operator's override is their business; a dead pin in a tracked file is everybody's.
ENV_FILE="${HAF_FYP_ENV_FILE:-${REPO_ROOT}/docker/.env}"

# Both files, because both carry pins that rot. docker-compose-ci.yml is what the test lanes
# themselves run on (pgvector, postgres, postgrest, python), so a tag disappearing there
# breaks CI rather than an operator.
COMPOSE_FILES="${HAF_FYP_COMPOSE_FILES:-${REPO_ROOT}/docker/docker-compose.yml ${REPO_ROOT}/docker/docker-compose-ci.yml}"

INSPECT_TIMEOUT="${HAF_FYP_INSPECT_TIMEOUT:-60}"
INSPECT_RETRIES="${HAF_FYP_INSPECT_RETRIES:-3}"

# STDERR, all of it. An earlier revision logged to stdout, and because the extraction runs in
# a command substitution its own error message was captured AS THE IMAGE LIST -- the job then
# reported seven unresolved images with names like "such" and "file:", exiting 1 for entirely
# the wrong reason. A diagnostic that can be mistaken for data is worse than none.
log() { printf '[check-pinned-images] %s\n' "$*" >&2; }
die() { log "$@"; log "Nothing was fully checked, which is NOT a pass."; exit 1; }

_manifest=""
_errfile="$(mktemp)" || { log "ERROR: could not create a temporary file"; exit 1; }
trap 'rm -f "${_errfile}" "${_manifest}"' EXIT
_manifest="$(mktemp)" || { log "ERROR: could not create a temporary file"; exit 1; }

for _file in ${COMPOSE_FILES}; do
    [ -f "${_file}" ] || die "ERROR: no such compose file: ${_file}"
done
[ -f "${ENV_FILE}" ] || die "ERROR: no such env file: ${ENV_FILE}"

# COMPOSE READS THE SHELL ENVIRONMENT IN PREFERENCE TO --env-file, so every key the env file
# defines is unset for the compose calls below. That is not a quirk being papered over:
# docker/.env says so itself ("a shell variable beats any --env-file"), and this repository
# positively invites operators to export HAF_VERSION. A GitLab CI/CD variable of that name
# would have this lane resolve some other tag and report green while the pin the repository
# ships is dead. Measured: with HAF_VERSION=develop exported, compose resolved
# .../haf:develop instead of .../haf:1.29.0-rc2, and `env -u HAF_VERSION` put it back.
ENV_UNSET=""
for _key in $(sed -n 's/^[[:space:]]*\([A-Za-z_][A-Za-z0-9_]*\)[[:space:]]*=.*/\1/p' \
                  "${ENV_FILE}" | sort -u); do
    ENV_UNSET="${ENV_UNSET} -u ${_key}"
done

# Unquoted on purpose (both are lists this script builds itself), and safe from globbing
# because of `set -f` above.
compose_at() {
    _f="$1"
    shift
    # shellcheck disable=SC2086
    env ${ENV_UNSET} docker compose --env-file "${ENV_FILE}" -f "${_f}" "$@"
}

# Services that PULL. `config --images` cannot be used: it prints a synthesised name for every
# build-only service (haf_fyp-haf-fyp-setup, ...), which exists in no registry, so the check
# would report five failures on a perfectly healthy stack and be switched off within a week. A
# service carrying `image:` AND `build:` (the rewriter) is kept -- compose pulls it and falls
# back to building only when the pull fails, which is the very thing worth knowing about.
#
# Every profile the file declares is enabled first. `docker compose config` OMITS a service
# whose profile is not, and docker/docker-compose.yml already has one (haf-fyp-uninstall).
# That service is build-only today so this changes nothing today -- but the day a profiled
# service gains an `image:`, it would be skipped in silence.
compose_images_of() {
    _f="$1"
    if ! _profiles="$(compose_at "${_f}" config --profiles 2>"${_errfile}")"; then
        log "ERROR: could not list the profiles of ${_f}"
        sed 's/^/    /' "${_errfile}" >&2
        return 1
    fi
    _profargs=""
    for _profile in ${_profiles}; do
        _profargs="${_profargs} --profile ${_profile}"
    done
    # shellcheck disable=SC2086
    if ! _json="$(compose_at "${_f}" ${_profargs} config --format json 2>"${_errfile}")"; then
        log "ERROR: \`docker compose config\` failed for ${_f}"
        sed 's/^/    /' "${_errfile}" >&2
        return 1
    fi
    if ! _one="$(printf '%s\n' "${_json}" | jq -r \
            '.services | to_entries[] | select(.value.image != null) | .value.image')"; then
        log "ERROR: could not extract images from ${_f}"
        return 1
    fi
    # PER FILE, not once at the end. The all-or-nothing check this replaced passed happily
    # when one of two compose files contributed nothing: the other file's images were
    # checked, the job said "checked 4 image(s), 0 unresolved", and the HAF pin -- the whole
    # reason for #56 -- was never contacted.
    if [ -z "${_one}" ]; then
        log "ERROR: ${_f} yielded no images to check."
        return 1
    fi
    printf '%s\n' "${_one}"
}

# The images the compose files pull are not all of them. Dockerfile.rewriter's base is
# common-ci-configuration/nginx:9f5e12cd... -- a 40-character commit tag, precisely the shape
# docker/.env warns gets swept (hive's policy keeps release tags forever and everything else
# only while it is one of the five newest or younger than a week). Nothing resolved it before.
#
# Each Dockerfile here has exactly one FROM, with no build stages and no ARG in the reference,
# so this stays deliberately simple -- and says out loud when it meets something it cannot
# resolve rather than skipping it quietly.
dockerfile_images_of() {
    _d="$1"
    _stages="$(sed -n \
        's/^[[:space:]]*[Ff][Rr][Oo][Mm][[:space:]].*[[:space:]][Aa][Ss][[:space:]][[:space:]]*\([^[:space:]][^[:space:]]*\).*/\1/p' \
        "${_d}")"
    sed -n 's/^[[:space:]]*[Ff][Rr][Oo][Mm][[:space:]][[:space:]]*\([^[:space:]][^[:space:]]*\).*/\1/p' \
        "${_d}" \
    | while IFS= read -r _ref; do
        case "${_ref}" in
            *'$'*)
                log "NOTICE: ${_d} FROM ${_ref} interpolates, so it is not resolved here"
                continue
                ;;
            scratch) continue ;;
        esac
        _is_stage=0
        for _stage in ${_stages}; do
            [ "${_ref}" = "${_stage}" ] && _is_stage=1
        done
        [ "${_is_stage}" -eq 1 ] && continue
        printf '%s\n' "${_ref}"
    done
}

_all=""
for _file in ${COMPOSE_FILES}; do
    _one="$(compose_images_of "${_file}")" || die "ERROR: ${_file} could not be read."
    _all="${_all}${_one}
"
done

if [ -n "${HAF_FYP_DOCKERFILES+x}" ]; then
    _dockerfiles="${HAF_FYP_DOCKERFILES}"
else
    _dockerfiles="$(find "${REPO_ROOT}" -maxdepth 1 -name 'Dockerfile.*' | sort)"
fi
for _file in ${_dockerfiles}; do
    [ -f "${_file}" ] || die "ERROR: no such Dockerfile: ${_file}"
    _one="$(dockerfile_images_of "${_file}")"
    [ -n "${_one}" ] || die "ERROR: ${_file} yielded no FROM image to check."
    _all="${_all}${_one}
"
done

_refs="$(printf '%s' "${_all}" | sort -u)"
# Never silently green. If the extraction stops finding images -- a compose schema change, a
# jq filter that no longer matches -- this would otherwise pass while checking nothing at all,
# which is the shape of the problem #63 is about.
[ -n "${_refs}" ] || die "ERROR: no images were extracted at all."

# Three outcomes, because conflating them is how a check gets switched off: 0 resolved,
# 1 genuinely absent, 2 INCONCLUSIVE.
#
# Most of these references are Docker Hub, and anonymous manifest requests count against its
# per-source-IP rate limit, which a shared GitLab runner reaches routinely. Measured while
# writing this: python:3.11-slim came back `429 Too Many Requests` on an ordinary run.
# Reporting that as "a pinned image no longer resolves -- fix the pin" sends somebody to
# change a perfectly good pin, and a lane that cries wolf gets disabled. So anything that does
# not positively say the manifest is gone is retried, then reported in its own words.
#
# `timeout` because this runs unattended: the project's default job timeout is an hour, and a
# registry that accepts the connection and never answers would hold a runner for all of it.
# BusyBox `timeout` sends TERM, so an expiry surfaces as 143, not GNU's 124 -- either way it
# is not a "manifest unknown", so it lands in INCONCLUSIVE, which is the point of classifying
# on the registry's words rather than on an exit status.
resolve() {
    _ref="$1"
    _try=1
    while :; do
        if timeout "${INSPECT_TIMEOUT}" docker buildx imagetools inspect \
                --format '{{json .Manifest}}' "${_ref}" >"${_manifest}" 2>"${_errfile}"; then
            return 0
        fi
        if grep -qiE 'not found|manifest unknown|no such manifest|does not exist|denied' \
                "${_errfile}"; then
            return 1
        fi
        [ "${_try}" -ge "${INSPECT_RETRIES}" ] && return 2
        sleep $((_try * 5))
        _try=$((_try + 1))
    done
}

_failed=0
_inconclusive=0
_checked=0

for _ref in ${_refs}; do
    _checked=$((_checked + 1))
    resolve "${_ref}" && _rc=0 || _rc=$?
    if [ "${_rc}" -eq 1 ]; then
        _failed=$((_failed + 1))
        log "UNRESOLVED ${_ref}"
        sed 's/^/    /' "${_errfile}" >&2
        continue
    fi
    if [ "${_rc}" -eq 2 ]; then
        _inconclusive=$((_inconclusive + 1))
        log "INCONCLUSIVE ${_ref} -- the registry did not say whether this exists"
        sed 's/^/    /' "${_errfile}" >&2
        continue
    fi

    # `{{json .Manifest}}`, and then a shape check, because buildx does NOT reject a template
    # it cannot resolve: `--format '{{.Manifest.Digest}}'` and `--format '{{.Manifest.digest}}'`
    # both exit 0 and print the DEFAULT human block instead (Name:/MediaType:/Digest:) --
    # measured against buildx v0.16.2. A typo there would have this printing three lines of
    # prose per image and passing forever.
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

    # The DIGEST OF THE MANIFEST LIST, which is what `docker pull` records and what a re-point
    # changes. `docker manifest inspect --verbose` gives PER-PLATFORM digests instead -- for
    # postgrest/postgrest:v12.2.3 that is sha256:bd918a7d..., while the list is
    # sha256:729bf65c... -- so reporting that one would make a re-pointed tag look unchanged.
    # `stable` was re-pointed upstream without its created_at moving; a digest in the log makes
    # that visible as a change between runs instead of an unbroken row of green.
    printf '%s %s\n' "${_digest}" "${_ref}"
done

log "checked ${_checked} image(s): ${_failed} unresolved, ${_inconclusive} inconclusive"

if [ "${_failed}" -ne 0 ]; then
    log "A pinned image no longer resolves. A fresh \`docker compose up\` dies on the pull;"
    log "a host with the image cached keeps working until it pulls again. Fix the pin in"
    log "${ENV_FILE}, the compose file, or the Dockerfile, then re-run."
    exit 1
fi
if [ "${_inconclusive}" -ne 0 ]; then
    log "The registry did not answer for the image(s) above, after ${INSPECT_RETRIES} tries."
    log "This is NOT a statement that the pin is dead -- do not change it on this evidence."
    log "Docker Hub rate-limits anonymous requests per source IP, which a shared runner"
    log "reaches routinely. Re-run; if it persists, the registry or the network is the story."
    # 75, not 1, and the lane declares `allow_failure: exit_codes: [75]` -- EX_TEMPFAIL, which
    # is exactly what this is. It keeps the distinction the classification above exists to
    # draw: a dead pin exits 1 and fails the pipeline, while "the registry would not say"
    # shows as a warning. Both are visible; only one is an accusation. A blanket
    # allow_failure would have made the whole lane decorative, which is what #56 already had.
    exit 75
fi
