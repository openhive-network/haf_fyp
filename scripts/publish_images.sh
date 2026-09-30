#!/bin/sh
# Mirror a release's three images to registry-upload.hive.blog (#72).
#
# publish_tag (the release stage) retags the tested :<short-sha> images as :<tag> in the
# GitLab registry. That is enough for anyone pulling from registry.gitlab.syncad.com, but
# haf_api_node's haf_fyp module resolves its images against
# ${HIVE_API_NODE_REGISTRY:-registry.hive.blog}/haf_fyp/{sql,python,postgrest-rewriter} --
# which 404 until something puts them there, so every node has to override all three.
# This is that something: it runs in the publish stage, after publish_tag, and copies
# :<tag> across.
#
#     registry.gitlab.syncad.com/hive/haf_fyp/sql:<tag>  ->  registry-upload.hive.blog/haf_fyp/sql:<tag>
#     ... and the same for python and postgrest-rewriter
#
# Written after common-ci-configuration's .docker_publish_job_template, which the sibling
# apps (haf_stats, balance_tracker) extend. This file includes nothing from common-ci --
# tests/unit/test_publish_tag_lane.py refuses an include:, because included rules are
# invisible to its model -- so the behaviour is restated here, where it can be tested:
# password-stdin logins, pull BY TAG (the name publish_tag created, not a SHA), and a
# retried push. Two differences, both deliberate:
#
#   * ALL THREE ARE PULLED AND TAGGED BEFORE ANY IS PUSHED, as publish_tag does. The three
#     are consumed as a set, so an image missing from the GitLab registry stops the job
#     before hive.blog sees anything. A PUSH that still fails after its retries can leave
#     the earlier images mirrored and the later ones not; a re-run completes it (pull, tag
#     and push are idempotent -- the digests do not change).
#   * The hive.blog credentials are REQUIRED. The template logs in only if they are set and
#     then pushes anyway, unauthenticated, so their absence surfaces as a push failure; the
#     job is gated on them, and this refuses early if the gate is ever bypassed.
#
# POSIX sh, not bash: the job image is docker:24, whose /bin/sh is BusyBox ash.
set -eu

IMAGES="sql python postgrest-rewriter"
TARGET_REGISTRY="${HIVE_BLOG_REGISTRY:-registry-upload.hive.blog}"
TARGET_PATH="${PUBLISH_PROJECT_NAME:-haf_fyp}"
PUSH_TRIES="${PUBLISH_PUSH_TRIES:-5}"

log() { printf '[publish-images] %s\n' "$*" >&2; }
die() { log "$@"; exit 1; }

required() {
    [ -n "$2" ] || die "ERROR: $1 is not set; nothing was published."
}
required CI_COMMIT_TAG "${CI_COMMIT_TAG:-}"
required CI_REGISTRY "${CI_REGISTRY:-}"
required CI_REGISTRY_IMAGE "${CI_REGISTRY_IMAGE:-}"
required CI_REGISTRY_USER "${CI_REGISTRY_USER:-}"
required CI_REGISTRY_PASSWORD "${CI_REGISTRY_PASSWORD:-}"
required BLOG_REGISTRY_USER "${BLOG_REGISTRY_USER:-}"
required BLOG_REGISTRY_PASSWORD "${BLOG_REGISTRY_PASSWORD:-}"

TAG="${CI_COMMIT_TAG}"
# The same refusal publish_tag makes: "/" is not valid in a docker tag, and renaming the
# release would publish it under a name nobody looks for.
case "${TAG}" in
    */*) die "ERROR: tag '${TAG}' contains '/', which is not a valid docker tag." ;;
esac
case "${PUSH_TRIES}" in
    '' | *[!0-9]*) die "ERROR: PUBLISH_PUSH_TRIES must be a whole number, got '${PUSH_TRIES}'." ;;
esac
[ "${PUSH_TRIES}" -ge 1 ] || die "ERROR: PUBLISH_PUSH_TRIES must be at least 1, got '${PUSH_TRIES}'."

# The password goes to docker on STDIN: printf is a shell builtin, so it never appears in
# a process list, and `docker login -p` would both expose it there and print docker's own
# warning into the job log.
login() {
    printf '%s\n' "$2" | docker login -u "$1" --password-stdin "$3" >/dev/null
}

# Retried with the template's backoff (10s, 20s, 40s, 80s): the external registry is
# known to be flaky under load, and a release is written once -- nothing re-runs it.
retried() {
    _n=1
    _delay=10
    while :; do
        if "$@"; then
            return 0
        fi
        if [ "${_n}" -ge "${PUSH_TRIES}" ]; then
            return 1
        fi
        log "attempt ${_n} of ${PUSH_TRIES} failed; retrying in ${_delay}s"
        sleep "${_delay}"
        _n=$((_n + 1))
        _delay=$((_delay * 2))
    done
}

login "${CI_REGISTRY_USER}" "${CI_REGISTRY_PASSWORD}" "${CI_REGISTRY}" \
    || die "ERROR: could not log in to ${CI_REGISTRY}."
retried login "${BLOG_REGISTRY_USER}" "${BLOG_REGISTRY_PASSWORD}" "${TARGET_REGISTRY}" \
    || die "ERROR: could not log in to ${TARGET_REGISTRY} after ${PUSH_TRIES} attempts."

# Pull BY TAG, not by SHA: :<tag> is what publish_tag published in the GitLab registry, so
# what lands on hive.blog is exactly the release people can already pull from there. A
# missing one fails here, before anything reaches hive.blog.
for image in ${IMAGES}; do
    source="${CI_REGISTRY_IMAGE}/${image}:${TAG}"
    target="${TARGET_REGISTRY}/${TARGET_PATH}/${image}:${TAG}"
    docker pull "${source}" || die "ERROR: could not pull ${source}; nothing was mirrored."
    docker tag "${source}" "${target}" || die "ERROR: could not tag ${source} as ${target}."
done

for image in ${IMAGES}; do
    target="${TARGET_REGISTRY}/${TARGET_PATH}/${image}:${TAG}"
    retried docker push "${target}" \
        || die "ERROR: pushing ${target} failed after ${PUSH_TRIES} attempts."
    log "mirrored ${target}"
done

log "mirrored all of ${IMAGES} at ${TAG} to ${TARGET_REGISTRY}/${TARGET_PATH}"
