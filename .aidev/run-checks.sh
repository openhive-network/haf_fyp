#!/usr/bin/env bash
# The step functions are called indirectly, through `step`.
# shellcheck disable=SC2317,SC2329
# The checks AIDEV's verification slots run (.aidev/project.yaml), as one junit
# report per suite: each named step is a test case, its log the failure body.
#
#   .aidev/run-checks.sh <suite> <step>...
#
# Every step but stack-pins and aidev-scripts is a job of .gitlab-ci.yml, run from
# that file by .aidev/ci_job.py, so what AIDEV runs moves with CI:
#
#   - stack-pins        the test stack and the runtime image still run what CI runs
#                       (.aidev/stack_pins.py)
#   - aidev-scripts     ShellCheck and a byte-compile over .aidev/'s own scripts
#   - sql-lint, shell-lint, python-lint, hadolint, compose-validate
#                       the lint jobs, verbatim
#   - openapi-drift     the openapi-drift job in a scratch copy of the tree (it
#                       regenerates endpoints/), offline: the runtime image carries the
#                       generator at the job's COMMON_CI_REF
#   - unit              python-tests: pytest tests/unit with its coverage floor; one
#                       junit case per test in unit/python-tests-1.xml and coverage.xml
#   - fastapi           fastapi-tests against the stack's fastapi-db: pytest
#                       tests/integration, then uvicorn and the FastAPI tavern file
#   - postgrest         postgrest-tests: docker/docker-compose-ci.yml's setup and tavern
#                       against the stack's database, PostgREST and rewriter
#   - install           install-test: docker/docker-compose-ci-install.yml's install /
#                       locked install / reinstall / uninstall chain over the model of HAF
#   - install-haf       install-test-haf: the same chain over the stack's HAF
#
# The last four need the stack (.aidev/test-stack.compose.yml); the compose lanes run
# through .aidev/compose_lane.py. Reports go to test-results/<suite>/: junit.xml (one
# case per step) and the pytest/tavern junit files the jobs write.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

suite="${1:?usage: $0 <suite> <step>...}"; shift
out="test-results/$suite"
rm -rf "$out"; mkdir -p "$out"
cases="$out/cases.tsv"; : > "$cases"

status=0
# record CASES_FILE NAME RC SECONDS LOG
record() {
    if [ "$3" -eq 0 ]; then
        printf 'case\t%s\tpass\t%s\t\n' "$2" "$4" >> "$1"
    else
        printf 'case\t%s\tfail\t%s\texit %s\t%s\n' "$2" "$4" "$3" "$5" >> "$1"
    fi
}

step() {
    local name="$1"; shift
    local log="$out/$name.log" t0=$SECONDS rc=0
    echo "== $name" >&2
    "$@" > "$log" 2>&1 < /dev/null || rc=$?
    [ "$rc" -eq 0 ] || { status=1; tail -40 "$log" >&2; }
    record "$cases" "$name" "$rc" "$((SECONDS - t0))" "$log"
}

job() { python3 .aidev/ci_job.py "$@" --out "$out"; }

aidev_scripts() {
    shellcheck -f gcc .aidev/*.sh .aidev/runtime/*.sh && python3 -m py_compile .aidev/*.py && echo "clean"
}

for s in "$@"; do
    case "$s" in
        stack-pins) step stack-pins python3 .aidev/stack_pins.py ;;
        aidev-scripts) step aidev-scripts aidev_scripts ;;
        sql-lint | python-lint) step "$s" job "$s" --path /opt/lint/bin ;;
        shell-lint | hadolint | compose-validate) step "$s" job "$s" ;;
        openapi-drift)
            step openapi-drift job openapi-drift --path /opt/openapi/bin --copy-tree \
                --env COMMON_CI_OFFLINE=1 --seed /opt/openapi/process_openapi.py=scripts/process_openapi.py ;;
        unit) step unit job python-tests --coverage-xml "$out/coverage.xml" --env "COVERAGE_FILE=$PWD/$out/.coverage" ;;
        fastapi) step fastapi job fastapi-tests --env "POSTGRES_HOST=${HAF_FYP_TEST_FASTAPI_DB:-fastapi-db}" ;;
        postgrest)
            step postgrest job postgrest-tests --compose-lane --path /opt/tavern/bin \
                --env "PYTEST_ADDOPTS=--junitxml=$PWD/$out/postgrest-tavern.xml" ;;
        install) step install job install-test --compose-lane ;;
        install-haf) step install-haf job install-test-haf --compose-lane ;;
        *) echo "unknown step: $s" >&2; exit 2 ;;
    esac
done
python3 .aidev/junit_cases.py "$out/junit.xml" "$suite" "$cases"
exit "$status"
