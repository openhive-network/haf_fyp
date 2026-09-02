#! /bin/bash
# Verb dispatcher for the haf_fyp SQL image (#27).
#
# Every haf_api_node app image exposes the same verbs, so a module file can say
# `command: [install_app, --swagger-url=...]` instead of naming a script path inside
# someone else's image. Without this, haf_fyp's compose services had to spell out
# `/app/scripts/install_app.sh`, which is why haf_fyp could not be deployed as a
# standard module.
#
# Modelled on balance_tracker/docker/scripts/docker_entrypoint.sh, including the
# --host/--user forwarding: the scripts here now accept those flags and map them onto
# the environment variables scripts/common.sh reads, so a runbook or module file
# copied from an upstream app works unchanged. (An earlier revision of this file
# refused to forward them, reasoning that install_app.sh silently ignored unrecognised
# flags -- true at the time, fixed in this same MR, and the wrong conclusion either
# way: the fix is to honour the flags, not to withhold them.)

set -euo pipefail

# /app is where Dockerfile.sql puts everything and is the only value that matters in
# production. It is a variable purely so tests/unit/test_docker_entrypoint.py can
# point the dispatcher at stub scripts and assert which one gets exec'd -- otherwise
# this file, which decides what every container from this image runs, would have no
# coverage at all outside a full image build.
APP_DIR="${HAF_FYP_APP_DIR:-/app}"

# Prints to STDOUT. The error paths below redirect it to stderr themselves; --help is
# a successful request for the text and belongs on stdout, where it can be piped and
# grepped. The CI image smoke test does exactly that, and caught this the first time it
# ran -- the whole reason that job now starts a container instead of only building one.
usage() {
    cat <<EOF
usage: $0 <verb> [args...]

verbs:
  install_app [--swagger-url=HOST] [--is_forking=FALSE] [--host=] [--port=] [--user=]
                                     install/upgrade schema, endpoints and roles
  process_blocks                     run the block-processing loop (blocks forever)
  uninstall_app [--force] [--host=] [--port=] [--user=]
                                     remove context, schemas and roles (DESTRUCTIVE)

Connection settings default from the environment -- POSTGRES_HOST (haf),
POSTGRES_PORT (5432), POSTGRES_DB (haf_block_log), POSTGRES_USER -- and the flags
above override them.

Anything else that is runnable -- a script path, a command on PATH -- is exec'd as
given, so the older command: ["/app/scripts/install_app.sh"] form still works. An
argument starting with "-" goes to bash, preserving the ad-hoc
\`docker run <image> -c '...'\` form the previous /bin/bash entrypoint allowed.
EOF
}

if [ $# -eq 0 ]; then
    usage >&2
    exit 1
fi

verb="$1"
shift

case "$verb" in
    install_app)
        exec "${APP_DIR}/scripts/install_app.sh" --host="${POSTGRES_HOST:-haf}" "$@"
        ;;
    process_blocks)
        # No startup-timestamp write here, deliberately -- scripts/process_blocks.sh
        # does it, so the compatibility path below gets it too. See the comment there.
        exec "${APP_DIR}/scripts/process_blocks.sh" --host="${POSTGRES_HOST:-haf}" "$@"
        ;;
    uninstall_app)
        exec "${APP_DIR}/scripts/uninstall_app.sh" \
            --host="${POSTGRES_HOST:-haf}" --user="${POSTGRES_USER:-haf_admin}" "$@"
        ;;
    --help|-h|-\?)
        # Ahead of the -* branch below, which would otherwise send the one flag every
        # operator tries straight to `bash --help` and leave this script's own usage
        # unreachable.
        usage
        exit 0
        ;;
    -*)
        # The previous entrypoint was a bare /bin/bash, so `docker run <image> -c
        # 'psql ...'` was the natural ad-hoc form for poking at a node. Preserved
        # rather than quietly broken; no verb starts with a dash, so this branch
        # cannot shadow one.
        exec /bin/bash "$verb" "$@"
        ;;
    *)
        # Anything runnable is still run, because `command: ["/app/scripts/process_blocks.sh"]`
        # is what every existing haf_fyp deployment passes today and it has to keep
        # working THROUGH the migration rather than after it.
        #
        # `type -P`, not `command -v`. Both resolve absolute paths and PATH entries,
        # but command -v also succeeds for shell BUILTINS: `cd` resolves to the bare
        # string "cd", which exec then fails on with 127 and no explanation. type -P
        # searches only for executable files, so builtins fall through to the usage
        # message where a mistyped verb belongs -- while `echo`, which is both a
        # builtin and /usr/bin/echo, still resolves to the file and runs.
        if resolved="$(type -P "$verb")" && [ -n "${resolved}" ]; then
            exec "${resolved}" "$@"
        fi

        # type -P requires the executable bit, which the old `ENTRYPOINT ["/bin/bash"]`
        # did not: it ran `bash <path>` regardless of mode. A bind-mounted script
        # without +x therefore used to work and would otherwise stop working here, so
        # an existing readable FILE is handed to bash the way it used to be.
        if [ -f "$verb" ] && [ -r "$verb" ]; then
            exec /bin/bash "$verb" "$@"
        fi
        echo "unknown verb: $verb" >&2
        usage >&2
        exit 1
        ;;
esac
