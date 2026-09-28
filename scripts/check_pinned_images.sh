#!/bin/sh
# Resolve every container image this repository pins, and fail unless EVERY one is positively
# found (#71, which supersedes #63).
#
# Nothing else notices a dead pin. compose-validate runs `docker compose config`, which parses
# YAML and never contacts a registry; docker-compose-ci.yml runs plain PostgreSQL with
# pgvector, so no CI lane pulls HAF; the guards in tests/unit/test_compose_contract.py are
# offline by design. That is how #56 lived: the stack asked for an image nobody had published,
# and every pipeline stayed green until an operator hit it on a cold cache.
#
# POSIX sh, not bash: the job's image is docker:24, which is Alpine, whose /bin/sh is BusyBox
# ash. shell-lint reads this directory, and the shebang is what tells it so.
#
# HOW A REFERENCE IS RESOLVED: one manifest HEAD against the registry's HTTP API, with the
# anonymous token its own WWW-Authenticate challenge points at. Measured against both
# registries this repository uses:
#
#   an existing tag     200, and Docker-Content-Digest is the MANIFEST-LIST digest -- what
#                       `docker pull` records and what a re-point changes (postgrest v12.2.3:
#                       sha256:729bf65c..., not a per-platform one)
#   a missing tag       404, carrying Docker-Distribution-Api-Version (a GET's body says
#                       MANIFEST_UNKNOWN / "manifest unknown")
#   a missing repo      Docker Hub: a token, then 401 on the manifest. gitlab.syncad.com: the
#                       token service itself refuses, 403 DENIED. Either way the same answer a
#                       private repository gets, and for an anonymous pull the same outcome.
#
# A refusal counts only if it holds on every try, and a 404 only if the registry itself sent
# it; anything else is asked again, then reported UNANSWERED.
#
# WHY HEAD, and why that matters more than anything else here. The first attempt (!77) used
# `docker buildx imagetools inspect`, a GET, and Docker Hub rate-limits anonymous GETs per
# source IP -- which a shared runner reaches routinely -- rejecting at the limiter BEFORE it
# looks the tag up. A dead tag and a rate-limited request then return the identical 429, so
# the lane "forgave" 429 as inconclusive and with it the dead pins it existed to catch: one run
# positively verified 3 of 10 images and reported success. Docker Hub does not count HEAD
# requests against that limit -- measured: RateLimit-Remaining stayed at 100 across HEADs and
# dropped to 99 on one GET -- so this sweep does not spend the budget it would trip over, and
# nothing here needs forgiving.
#
# SO THERE IS NO "INCONCLUSIVE" PASS. The job succeeds only when every reference was
# positively resolved. A reference the registry would not answer for, after retries, fails the
# job as loudly as a dead one -- with its own message, because only one of the two is a
# statement about the pin. The summary says how many were PROVEN, never how many were merely
# not disproven.
#
# NO LOGIN, deliberately: these are public repositories and anonymous is how an operator pulls
# them. Authenticating would hide a repository that has become unreadable to everyone else.
set -euf

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH='' cd -- "${SCRIPT_DIR}/.." && pwd)"

# docker/.env, not .env.local: this resolves what the repository SHIPS.
ENV_FILE="${HAF_FYP_ENV_FILE:-${REPO_ROOT}/docker/.env}"
# Both files: docker-compose-ci.yml is what the test lanes themselves run on.
COMPOSE_FILES="${HAF_FYP_COMPOSE_FILES:-}"
# An empty override must not mean "no Dockerfiles" (!77 skipped all three with exit 0 that
# way): unset or blank, the repository's Dockerfile.* are found, and finding none is an error
# below, not a pass.
DOCKERFILES="${HAF_FYP_DOCKERFILES:-}"

TRIES="${HAF_FYP_RESOLVE_TRIES:-3}"
REQUEST_TIMEOUT="${HAF_FYP_REQUEST_TIMEOUT:-20}"
# The whole sweep's budget, and the job's `timeout:` is set ABOVE it (the lane test holds the
# two together). Per reference the worst case is TRIES x (HEAD + token + HEAD) x
# REQUEST_TIMEOUT plus backoff -- over three minutes -- so without a deadline ten references
# against a hung registry outlast any sane job timeout, and the failure arrives as a bare
# "job timed out" instead of this script's account of what it proved. Past the deadline the
# remaining references are reported unanswered, which fails the job.
DEADLINE="${HAF_FYP_RESOLVE_DEADLINE:-420}"
# Where references with no registry go -- Docker Hub's registry host, as Docker resolves it.
DOCKER_HUB="registry-1.docker.io"
# Only the tests change this, to reach a local fake registry over plain HTTP; the hosts
# themselves are real names, routed there by curl's own config.
SCHEME="${HAF_FYP_REGISTRY_SCHEME:-https}"

# Everything goes to STDERR except the one line per resolved image. The extraction runs in
# command substitutions, and !77 once captured its own error message AS THE IMAGE LIST.
log() { printf '[check-pinned-images] %s\n' "$*" >&2; }
die() { log "$@"; log "Nothing was fully checked, which is NOT a pass."; exit 1; }

# Each is used in arithmetic or handed to curl/sleep; a bad value must stop the script here
# with a message, not later with an expansion error.
whole_number() {
    case "$2" in
        '' | *[!0-9]*) die "ERROR: $1 must be a whole number, got '$2'" ;;
    esac
    [ "$2" -ge 1 ] || die "ERROR: $1 must be at least 1, got '$2'"
}
whole_number HAF_FYP_RESOLVE_TRIES "${TRIES}"
whole_number HAF_FYP_REQUEST_TIMEOUT "${REQUEST_TIMEOUT}"
whole_number HAF_FYP_RESOLVE_DEADLINE "${DEADLINE}"

# BLANK counts as unset, not just empty. A CI/CD variable left as a single space expanded to
# an empty LIST under `:-` -- and a list with nothing in it is a loop that checks nothing:
# HAF_FYP_COMPOSE_FILES=" " once skipped both compose files and the HAF pin with exit 0.
blank() {
    case "$1" in
        *[![:space:]]*) return 1 ;;
        *) return 0 ;;
    esac
}
if blank "${COMPOSE_FILES}"; then
    COMPOSE_FILES="${REPO_ROOT}/docker/docker-compose.yml ${REPO_ROOT}/docker/docker-compose-ci.yml"
fi
if blank "${DOCKERFILES}"; then
    DOCKERFILES=""
fi

# Missing tools say so, rather than surfacing as "could not be read" or a page of `docker
# --help`. compose is checked the way it is used below -- in an emptied environment -- because
# a compose plugin found only through DOCKER_CONFIG disappears there.
for _tool in curl jq docker; do
    command -v "${_tool}" >/dev/null 2>&1 \
        || die "ERROR: ${_tool} is not installed (the CI job installs curl and jq with apk)."
done
env -i PATH="${PATH}" HOME="${HOME:-/root}" docker compose version >/dev/null 2>&1 \
    || die "ERROR: \`docker compose\` does not run in a clean environment (PATH and HOME" \
           "only). A compose plugin found only through DOCKER_CONFIG is not seen there;" \
           "install it under \$HOME/.docker/cli-plugins or system-wide."

_headers=""
_tokfile=""
_errfile="$(mktemp)" || { log "ERROR: could not create a temporary file"; exit 1; }
trap 'rm -f "${_errfile}" "${_headers}" "${_tokfile}"' EXIT
_headers="$(mktemp)" || { log "ERROR: could not create a temporary file"; exit 1; }
_tokfile="$(mktemp)" || { log "ERROR: could not create a temporary file"; exit 1; }

for _file in ${COMPOSE_FILES}; do
    [ -f "${_file}" ] || die "ERROR: no such compose file: ${_file}"
done
[ -f "${ENV_FILE}" ] || die "ERROR: no such env file: ${ENV_FILE}"

# COMPOSE READS THE SHELL ENVIRONMENT IN PREFERENCE TO --env-file (docker/.env says so
# itself), so a CI/CD variable or an operator's `export HAF_VERSION=...` would have this
# resolve some other tag and report green while the shipped pin is dead. !77 unset the keys
# docker/.env defines, which missed HAF_FYP_REWRITER_IMAGE -- the documented haf_api_node
# override, which docker/.env does not define -- and any `export KEY=value` line. So the
# environment is not filtered, it is REPLACED: compose sees PATH, HOME and nothing else.
compose_at() {
    _f="$1"
    shift
    env -i PATH="${PATH}" HOME="${HOME:-/root}" \
        docker compose --env-file "${ENV_FILE}" -f "${_f}" "$@"
}

# Services that PULL: `config --images` prints a synthesised name for every build-only
# service, which exists in no registry. A service with `image:` AND `build:` (the rewriter) is
# kept -- compose pulls it and builds only when the pull fails. Every profile the file
# declares is enabled first, or `config` omits profiled services in silence.
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
    # PER FILE: with two files, an all-or-nothing check once passed with one of them
    # contributing nothing -- and the HAF pin never contacted.
    if [ -z "${_one}" ]; then
        log "ERROR: ${_f} yielded no images to check."
        return 1
    fi
    printf '%s\n' "${_one}"
}

# Every FROM, the way Docker reads it: flags such as --platform=... are skipped, a reference
# to an EARLIER stage is not an image (stage names are case-insensitive), `scratch` is not
# one, and a reference that interpolates an ARG cannot be resolved here -- each of those is
# said out loud on stderr. A Dockerfile made only of those is legitimate and yields nothing;
# one with no FROM at all is broken, and exits 3.
dockerfile_images_of() {
    awk -v f="$1" '
        toupper($1) == "FROM" {
            nfrom++
            i = 2
            while (i <= NF && substr($i, 1, 2) == "--") i++
            if (i > NF) { print "NOTICE: " f ": FROM with no image: " $0 > "/dev/stderr"; next }
            ref = $i
            low = tolower(ref)
            if (low == "scratch") {
                print "NOTICE: " f ": FROM scratch is not an image" > "/dev/stderr"
            } else if (low in stage) {
                # a reference to an earlier stage
            } else if (index(ref, "$") > 0) {
                print "NOTICE: " f ": FROM " ref " interpolates, so it is not resolved here" > "/dev/stderr"
            } else {
                print ref
            }
            if (i + 2 <= NF && tolower($(i + 1)) == "as") stage[tolower($(i + 2))] = 1
        }
        END { if (nfrom == 0) exit 3 }
    ' "$1"
}

_all=""
for _file in ${COMPOSE_FILES}; do
    _one="$(compose_images_of "${_file}")" || die "ERROR: ${_file} could not be read."
    _all="${_all}${_one}
"
done

if [ -z "${DOCKERFILES}" ]; then
    DOCKERFILES="$(find "${REPO_ROOT}" -maxdepth 1 -name 'Dockerfile.*' | sort)"
fi
[ -n "${DOCKERFILES}" ] || die "ERROR: no Dockerfile.* found in ${REPO_ROOT}."
for _file in ${DOCKERFILES}; do
    [ -f "${_file}" ] || die "ERROR: no such Dockerfile: ${_file}"
    _rc=0
    _one="$(dockerfile_images_of "${_file}")" || _rc=$?
    [ "${_rc}" -eq 0 ] || die "ERROR: ${_file} has no FROM instruction."
    [ -z "${_one}" ] || _all="${_all}${_one}
"
done

_refs="$(printf '%s' "${_all}" | sed '/^$/d' | sort -u)"
[ -n "${_refs}" ] || die "ERROR: no images were extracted at all."

# Where a reference points: registry host, repository, and the tag or digest, as Docker
# resolves them -- a first component with a dot, a colon, or `localhost` is a registry;
# anything else is Docker Hub, where a bare name lives under library/.
split_ref() {
    _r="$1"
    _digest=""
    case "${_r}" in
        *@*) _digest="${_r#*@}"; _r="${_r%@*}" ;;
    esac
    _first="${_r%%/*}"
    _reg="${DOCKER_HUB}"
    _hub=1
    _path="${_r}"
    if [ "${_first}" != "${_r}" ]; then
        case "${_first}" in
            docker.io | index.docker.io) _path="${_r#*/}" ;;
            *.* | *:* | localhost) _reg="${_first}"; _hub=0; _path="${_r#*/}" ;;
        esac
    fi
    # Docker Hub's official images live under library/ -- on Hub only, and only because the
    # reference named no namespace. Another registry's single-component path is its own.
    if [ "${_hub}" -eq 1 ]; then
        case "${_path}" in
            */*) ;;
            *) _path="library/${_path}" ;;
        esac
    fi
    _tag="latest"
    _last="${_path##*/}"
    case "${_last}" in
        *:*) _tag="${_last#*:}"; _path="${_path%:*}" ;;
    esac
    REG="${_reg}"
    REPO="${_path}"
    REF="${_digest:-${_tag}}"
}

ACCEPT='application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json,application/vnd.docker.distribution.manifest.v2+json,application/vnd.oci.image.manifest.v1+json'

# Prints the HTTP status, or 000 when no response came back at all.
head_manifest() {
    _url="$1"
    _tok="${2:-}"
    set -- -sS -I -o /dev/null -D "${_headers}" -m "${REQUEST_TIMEOUT}" \
        -H "Accept: ${ACCEPT}" -w '%{http_code}'
    if [ -n "${_tok}" ]; then
        set -- "$@" -H "Authorization: Bearer ${_tok}"
    fi
    curl "$@" "${_url}" 2>"${_errfile}" || true
}

# One attempt. Sets:
#   STATUS         the manifest's HTTP status; "denied" when the registry's token service
#                  refused the repository; 000 when no answer came back
#   DIGEST         Docker-Content-Digest, when there was one
#   FROM_REGISTRY  1 when the response carries Docker-Distribution-Api-Version -- i.e. the
#                  registry itself answered, not a proxy or an ingress in front of it
#   RATE           the rate-limit and Retry-After headers, for the log when nothing resolves
#   TOKEN_STATUS   the token service's HTTP status, when it did not issue a token
read_rate() {
    RATE="$(tr -d '\r' < "${_headers}" | grep -iE '^(ratelimit-remaining|retry-after):' | tr '\n' ' ' || true)"
}

attempt() {
    DIGEST=""
    FROM_REGISTRY=0
    RATE=""
    TOKEN_STATUS=""
    _url="${SCHEME}://${REG}/v2/${REPO}/manifests/${REF}"
    : > "${_headers}"
    STATUS="$(head_manifest "${_url}")"
    if [ "${STATUS}" = "401" ]; then
        # The registry names its own token service. Parsed rather than hard-coded, so
        # auth.docker.io and gitlab.syncad.com/jwt/auth need no special cases.
        _chal="$(tr -d '\r' < "${_headers}" | grep -i '^www-authenticate:[[:space:]]*bearer' | head -n 1 || true)"
        _realm="$(printf '%s' "${_chal}" | sed -n 's/.*[Rr][Ee][Aa][Ll][Mm]="\([^"]*\)".*/\1/p')"
        _service="$(printf '%s' "${_chal}" | sed -n 's/.*[Ss][Ee][Rr][Vv][Ii][Cc][Ee]="\([^"]*\)".*/\1/p')"
        if [ -n "${_realm}" ]; then
            : > "${_tokfile}"
            : > "${_headers}"
            _tcode="$(curl -sS -G -m "${REQUEST_TIMEOUT}" -o "${_tokfile}" -D "${_headers}" \
                          -w '%{http_code}' "${_realm}" \
                          --data-urlencode "service=${_service}" \
                          --data-urlencode "scope=repository:${REPO}:pull" 2>"${_errfile}" \
                      || true)"
            if [ "${_tcode}" != "200" ]; then
                TOKEN_STATUS="${_tcode}"
                STATUS="000"     # no token: unanswered, not a verdict -- unless refused below
                # The TOKEN SERVICE refusing the repository IS a verdict: gitlab.syncad.com
                # answers a missing, renamed or private project with 403 and a registry error
                # body, {"errors":[{"code":"DENIED",...}]}, where Docker Hub issues a token
                # and the manifest request 401s. Only that body counts -- a WAF or a proxy
                # page answering 403 is not the registry speaking.
                case "${_tcode}" in
                    401 | 403)
                        if jq -e '[.errors[]?.code] | any(. == "DENIED" or . == "UNAUTHORIZED")' \
                                < "${_tokfile}" >/dev/null 2>&1; then
                            STATUS="denied"
                        fi
                        ;;
                esac
                read_rate
                return 0
            fi
            _tok="$(jq -r '.token // .access_token // empty' < "${_tokfile}" 2>/dev/null || true)"
            if [ -z "${_tok}" ]; then
                STATUS="000"
                TOKEN_STATUS="200 without a token in it"
                read_rate
                return 0
            fi
            : > "${_headers}"
            STATUS="$(head_manifest "${_url}" "${_tok}")"
        fi
    fi
    _h="$(tr -d '\r' < "${_headers}")"
    DIGEST="$(printf '%s\n' "${_h}" | sed -n 's/^[Dd]ocker-[Cc]ontent-[Dd]igest:[[:space:]]*//p' | head -n 1)"
    if printf '%s\n' "${_h}" | grep -qi '^docker-distribution-api-version:'; then
        FROM_REGISTRY=1
    fi
    read_rate
}

_start="$(date +%s)"
_total=0
_resolved=0
_gone=0
_unanswered=0

for _ref in ${_refs}; do
    _total=$((_total + 1))
    split_ref "${_ref}"
    _try=1
    _verdict=""
    _budget=""
    # Cleared by any try that is not a refusal from the registry itself, so UNREADABLE means
    # what it says: refused on EVERY try -- not refused on the last one after two outages.
    _refused_every_try=1
    while :; do
        if [ $(( $(date +%s) - _start )) -ge "${DEADLINE}" ]; then
            # A reference that was already asked keeps what it was told -- its last status,
            # rate-limit headers and curl error -- rather than reading as "not asked".
            if [ "${_try}" -gt 1 ]; then
                _verdict="unanswered"
                _budget="; the sweep's budget ran out before the next try"
            else
                _verdict="deadline"
            fi
            break
        fi
        attempt
        case "${STATUS}" in
            denied) ;;
            401 | 403) [ "${FROM_REGISTRY}" -eq 1 ] || _refused_every_try=0 ;;
            *) _refused_every_try=0 ;;
        esac
        case "${STATUS}" in
            200)
                case "${DIGEST}" in
                    sha256:*) _verdict="resolved"; break ;;
                esac
                ;;
            404)
                # Only the registry's own 404 is a verdict. One from a proxy or a misrouted
                # ingress would otherwise report every pin on that host as gone. Final at
                # once, unlike a refusal: neither registry has been seen to 404 a manifest
                # that exists, while auth services do refuse transiently.
                if [ "${FROM_REGISTRY}" -eq 1 ]; then
                    _verdict="missing"
                    break
                fi
                ;;
        esac
        # Everything else is asked again -- including a refusal (401, 403, DENIED), which
        # becomes a verdict only if it holds on every try: one bad answer from an auth service
        # must not accuse a healthy pin of being gone.
        if [ "${_try}" -ge "${TRIES}" ]; then
            if [ "${_refused_every_try}" -eq 1 ]; then
                _verdict="unreadable"
            else
                _verdict="unanswered"
            fi
            break
        fi
        sleep $((_try * 5))
        _try=$((_try + 1))
    done

    case "${_verdict}" in
        resolved)
            _resolved=$((_resolved + 1))
            printf '%s %s\n' "${DIGEST}" "${_ref}"
            ;;
        missing)
            _gone=$((_gone + 1))
            log "MISSING ${_ref}: ${REG} has no ${REPO}:${REF} (HTTP 404)"
            ;;
        unreadable)
            _gone=$((_gone + 1))
            if [ "${STATUS}" = "denied" ]; then
                log "UNREADABLE ${_ref}: ${REG}'s token service refuses ${REPO} on every try"
            else
                log "UNREADABLE ${_ref}: ${REG} refuses anonymous access to ${REPO} (HTTP ${STATUS} on every try)"
            fi
            log "    -- the repository is gone, renamed or private; an operator's pull fails the same way"
            ;;
        unanswered)
            _unanswered=$((_unanswered + 1))
            _why="HTTP ${STATUS}"
            case "${STATUS}" in
                404) [ "${FROM_REGISTRY}" -eq 1 ] \
                    || _why="HTTP 404 without the registry's own header" ;;
                401 | 403)
                    if [ "${FROM_REGISTRY}" -eq 1 ]; then
                        _why="HTTP ${STATUS}, but refused on only some tries"
                    else
                        _why="HTTP ${STATUS} without the registry's own header"
                    fi
                    ;;
                denied) _why="token service refusal, but on only some tries" ;;
                000) [ -z "${TOKEN_STATUS}" ] || _why="token service HTTP ${TOKEN_STATUS}" ;;
            esac
            log "UNANSWERED ${_ref}: no verdict from ${REG} after ${_try} of ${TRIES} tries (last: ${_why}${_budget})"
            [ -z "${RATE}" ] || log "    ${RATE}"
            [ ! -s "${_errfile}" ] || sed 's/^/    /' "${_errfile}" >&2
            ;;
        deadline)
            _unanswered=$((_unanswered + 1))
            log "UNANSWERED ${_ref}: not asked -- the ${DEADLINE}s budget for the sweep ran out"
            ;;
    esac
done

log "positively resolved ${_resolved} of ${_total}: ${_gone} missing or unreadable, ${_unanswered} unanswered"

if [ "${_gone}" -ne 0 ]; then
    log "A pinned image no longer resolves. A fresh \`docker compose up\` dies on the pull; a"
    log "host with it cached keeps working until it pulls again. Fix the pin in ${ENV_FILE},"
    log "the compose file, or the Dockerfile."
fi
if [ "${_unanswered}" -ne 0 ]; then
    log "Unanswered is NOT evidence that a pin is dead -- do not change one on it -- and it is"
    log "not a pass either: those images were not checked. Re-run; if it persists, the"
    log "registry or the runner's network is the story."
fi
[ "${_resolved}" -eq "${_total}" ] || exit 1
