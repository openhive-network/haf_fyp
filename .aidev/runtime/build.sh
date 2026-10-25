#!/usr/bin/env bash
# Build (and with --push, publish) the image AIDEV runs this project's checks in
# (.aidev/runtime/Dockerfile -> hive/haf_fyp/aidev-tests, .aidev/project.yaml's
# `environment.image`) and print the digest-pinned reference to put there. See
# .aidev/README.md.
#
# The tag is `aidev-<hash>`, the hash of the image inputs (its Dockerfile, every
# FROM digest-pinned, and requirements.txt). When
# <repository>:aidev-<hash> is already in the registry nothing is built: the script
# prints that image's digest.
#
#   .aidev/runtime/build.sh          # registry digest, or build locally and print the local tag
#   .aidev/runtime/build.sh --push   # registry digest, or build + push and print repo@sha256:<digest>
#   .aidev/runtime/build.sh --tag    # print the input-hash tag only
set -euo pipefail

cd "$(dirname "$0")/../.."

usage() {
    echo "usage: $0 [--push|--tag]" >&2
    exit 2
}

mode=""
for arg in "$@"; do
    case "$arg" in
        --push|--tag) mode="$arg" ;;
        *) usage ;;
    esac
done

DOCKERFILE=.aidev/runtime/Dockerfile
REPOSITORY="${AIDEV_RUNTIME_REPOSITORY:-registry.gitlab.syncad.com/hive/haf_fyp/aidev-tests}"

# The inputs: the Dockerfile and requirements.txt, the one file it copies from the tree
# (environment.inputs in .aidev/project.yaml names both).
input_hash() {
    sha256sum "$DOCKERFILE" requirements.txt | sha256sum | cut -c1-16
}
TAG="aidev-$(input_hash)"

if [ "$mode" = "--tag" ]; then
    echo "$TAG"
    exit 0
fi

# Digest of <repository>:<tag> in the registry, empty when the tag doesn't exist.
registry_digest() {
    docker buildx imagetools inspect "$REPOSITORY:$TAG" --format '{{json .Manifest}}' 2>/dev/null \
        | grep -o '"digest": *"sha256:[0-9a-f]*"' | head -1 | grep -o 'sha256:[0-9a-f]*' || true
}

digest="$(registry_digest)"
if [ -n "$digest" ]; then
    echo "$REPOSITORY:$TAG exists in the registry; not building" >&2
    echo "$REPOSITORY@$digest"
    exit 0
fi
echo "$REPOSITORY:$TAG is not in the registry; building it" >&2

# The context holds requirements.txt alone.
context="$(mktemp -d)"
trap 'rm -rf "$context"' EXIT
cp requirements.txt "$context/"

# Optionally build through a pull-through registry cache near the build host, so FROM
# stays canonical and the build doesn't compete for the uplink. Set
# AIDEV_IMAGE_CACHE_BY_REGION to region=host:port pairs (comma-separated); a pair
# applies when its region is one of the labels of this host's FQDN, and a `*` pair
# applies to any other host. Unset (the default), the image is built straight from
# registry.gitlab.syncad.com. Prints the builder name, or nothing when no cache applies.
region_builder() {
    local cache_map="${AIDEV_IMAGE_CACHE_BY_REGION:-}"
    local region="*" label entry cache="" builder config
    [ -n "$cache_map" ] || return 0
    for label in $( (hostname -f 2>/dev/null || hostname) | tr 'A-Z.' 'a-z '); do
        for entry in ${cache_map//,/ }; do
            if [ "${entry%%=*}" = "$label" ]; then region="$label"; fi
        done
    done
    for entry in ${cache_map//,/ }; do
        if [ "${entry%%=*}" = "$region" ]; then cache="${entry#*=}"; fi
    done
    if [ -z "$cache" ]; then
        echo "no image cache applies to this host; building straight from registry.gitlab.syncad.com" >&2
        return
    fi
    builder="aidev-region-build-${cache//[.:]/-}"
    if ! docker buildx inspect "$builder" >/dev/null 2>&1; then
        config="$(mktemp)"
        printf '[registry."%s"]\n  mirrors = ["%s"]\n\n[registry."%s"]\n  http = true\n  insecure = true\n' \
            "registry.gitlab.syncad.com" "$cache" "$cache" >"$config"
        docker buildx create --name "$builder" --driver docker-container --config "$config" >/dev/null
        rm -f "$config"
    fi
    echo "$builder"
}
builder="$(region_builder)"

# The FROM is digest-pinned, so no --pull. No provenance, so the pushed digest is a
# plain image manifest.
build=(docker buildx build ${builder:+--builder "$builder"} --provenance=false -f "$DOCKERFILE" -t "$REPOSITORY:$TAG")
if [ "$mode" != "--push" ]; then
    "${build[@]}" --load "$context" >&2
    echo "$REPOSITORY:$TAG"
    exit 0
fi

"${build[@]}" --push --metadata-file "$context/metadata.json" "$context" >&2
digest="$(grep -o '"containerimage.digest": *"sha256:[0-9a-f]*"' "$context/metadata.json" | grep -o 'sha256:[0-9a-f]*')"
echo "$REPOSITORY@${digest:?no digest in buildx metadata}"
