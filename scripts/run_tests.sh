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
COMPOSE="docker compose -f ${SCRIPT_DIR}/../docker/docker-compose-ci.yml"

KEEP=0
for arg in "$@"; do
    [[ "$arg" == "--keep" ]] && KEEP=1
done

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
${COMPOSE} wait tavern
EXIT_CODE=$?

echo "==> Tavern logs:"
${COMPOSE} logs tavern

if [[ $KEEP -eq 0 ]]; then
    echo "==> Tearing down..."
    ${COMPOSE} down -v
fi

echo "==> Tavern exited with code ${EXIT_CODE}"
exit "${EXIT_CODE}"
