#!/usr/bin/env bash
# Run the CI test stack and return Tavern's exit code.
#
# Uses detached mode so that the one-shot setup container exiting does
# not abort the rest of the stack (a Docker Compose limitation with
# --abort-on-container-exit).
#
# Usage:
#   ./scripts/run_tests.sh            # run tests, tear down on exit
#   ./scripts/run_tests.sh --keep     # leave stack running after tests
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# -p as well as the file's `name:`: a COMPOSE_PROJECT_NAME left in the shell outranks
# `name:`, and would send the `down -v --remove-orphans` below into that project (#53).
COMPOSE="docker compose -p haf_fyp_ci -f ${SCRIPT_DIR}/../docker/docker-compose-ci.yml"

KEEP=0
for arg in "$@"; do
    [[ "$arg" == "--keep" ]] && KEEP=1
done

# Set before the trap is installed: under `set -u` the trap reads them, and the earliest it
# can run is a failing `up`.
TAVERN_LOGGED=0

# The teardown runs from a trap, not from the end of the script (#55). Under `set -e` the
# end of the script is not reached when anything before it fails -- which is exactly when
# the stack is worth removing: a failed `up` left it half-started, and a failing `wait`
# ended the run before the teardown, so the next run's `down -v --remove-orphans` was what
# eventually cleaned up. A trap also covers Ctrl-C, which the usage line above implies.
#
# EVERY command in here is guarded. Under `set -e` a failing command inside the trap both
# ends the trap -- skipping whatever came after it, `down -v` included -- and replaces the
# status the script was exiting with. That is not hypothetical: `compose logs` on a service
# that was never created exits non-zero, which is the `up`-failed path this trap now covers.
# Measured: unguarded, a failing run reported 5 (the logs) instead of 4 (Tavern), and the
# stack was never removed. `set -o pipefail` is on, so a pipeline added here needs the guard
# on the whole pipeline, not on its last stage.
#
# Bash keeps the pending exit status across an EXIT trap that does not itself call `exit`,
# so the code computed below survives -- as long as nothing in here disturbs it.
teardown() {
    local code=$?
    # A signal arriving while this runs would re-enter the handlers below, discard `code`
    # and truncate the teardown. Measured: a TERM during the teardown turned Tavern's 4
    # into 143 and skipped `down -v`. Ignore both for the rest of the trap.
    trap '' INT TERM

    # The backgrounded `compose wait` outlives a signal that hit only this script.
    if [[ -n ${WAIT_PID:-} ]]; then
        kill "${WAIT_PID}" 2>/dev/null || true
    fi

    # What the run failed on is usually NOT in Tavern's logs. A SQL error in `setup` fails
    # `up` itself (the CI file gates every service on `service_completed_successfully`), so
    # the body below never runs and this trap would otherwise remove the only container
    # that holds the error. .gitlab-ci.yml's after_script prints these same three.
    if [[ ${code} -ne 0 ]]; then
        for service in setup tavern haf-fyp-postgrest-rewriter; do
            # tavern only if the body did not already print it -- on the `wait` path it did.
            [[ ${service} == tavern && ${TAVERN_LOGGED} -eq 1 ]] && continue
            echo "==> ${service} logs:"
            ${COMPOSE} logs "${service}" || true
        done
    fi

    # `[ ]`, not `[[ ]]`: under a shell without `[[` the test fails, the `else` branch runs,
    # and the script claims the operator asked for --keep on a run that did not.
    if [ "${KEEP}" -eq 0 ]; then
        echo "==> Tearing down..."
        ${COMPOSE} down -v --remove-orphans ||
            echo "==> Teardown FAILED; the CI stack may still be up. Run:" \
                 "${COMPOSE} down -v --remove-orphans" >&2
        if [[ ${code} -ne 0 ]]; then
            echo "==> To keep the stack next time and inspect it: ./scripts/run_tests.sh --keep"
        fi
    else
        echo "==> Leaving the stack up (--keep)."
    fi
}
trap teardown EXIT
# Without the INT handler a `kill -INT` aimed at this script alone is ignored while a
# foreground child runs (measured: the run completed, rc 0). TERM is redundant in bash --
# the EXIT trap runs anyway -- but spelled out so both codes are deliberate rather than
# inherited. SIGKILL and SIGPIPE still skip the teardown; nothing in a shell can change that.
trap 'exit 130' INT
trap 'exit 143' TERM

echo "==> Tearing down any previous run..."
${COMPOSE} down -v --remove-orphans 2>/dev/null || true

echo "==> Starting CI test stack (detached)..."
# --build, because THIS is the local entry point the compose header names. Without it
# compose only builds when the derived image is absent, so after the first run an edit
# to endpoints/rewrite_rules.conf or Dockerfile.rewriter is silently ignored and the
# rewriter stages go green on the OLD rules -- the exact failure they exist to catch.
# (It was first added to .gitlab-ci.yml, where dind caches nothing and it does nothing.)
${COMPOSE} up --detach --build

echo "==> Waiting for Tavern tests to complete..."
# `|| EXIT_CODE=$?` rather than a bare call followed by `EXIT_CODE=$?` (#55): `wait` exits
# with the container's status, so under `set -e` a FAILING test run ended the script right
# here -- before the logs, which are the only place the failure is described, and before
# the teardown. The assignment never ran either; the line that read it was unreachable.
#
# Backgrounded, and awaited with the `wait` BUILTIN, so a signal is acted on now rather than
# whenever Tavern finishes: bash runs no trap while a foreground child is running. Measured
# with a ten-second wait, TERM at t+2s -- foreground, the teardown started at t+11s; this
# way, t+3s. WAIT_PID is assigned on the next line, before anything can read it, and the
# teardown kills it, because a signal aimed at this script does not reach that child.
EXIT_CODE=0
${COMPOSE} wait tavern &
WAIT_PID=$!
wait "${WAIT_PID}" || EXIT_CODE=$?

# Both of these matter more when the run failed than when it passed, so neither may end it.
echo "==> Tavern logs:"
${COMPOSE} logs tavern || echo "==> (could not read Tavern's logs)"
TAVERN_LOGGED=1

echo "==> Tavern exited with code ${EXIT_CODE}"
exit "${EXIT_CODE}"
