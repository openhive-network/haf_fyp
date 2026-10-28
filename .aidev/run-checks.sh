#!/usr/bin/env bash
# The step functions are called indirectly, through `step`.
# shellcheck disable=SC2317,SC2329
# The checks AIDEV's verification slots run (.aidev/project.yaml), as one junit
# report per suite: each named step is a test case, its log the failure body.
#
#   .aidev/run-checks.sh <suite> <step>...
#
# Every step but stack-pins, aidev-scripts and the replay-* steps is a job of
# .gitlab-ci.yml, run from that file by .aidev/ci_job.py, so what AIDEV runs moves with CI:
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
#   - replay-install    two copy-on-write clones of the shared 5M-block HAF for this run
#                       (haf_shared.clone_run_database on AIDEV_EXTERNAL_HAF_HOST:_PORT,
#                       as haf_shared_consumer); in each a stub of the hivemind tables
#                       haf_fyp reads, built from the clone's own operations
#                       (tests/hivemind_stub_from_haf.sql: the shared HAF has no
#                       hivemind, #97) -- to block 4000000 in the timed clone, to 5000000
#                       in the plain one -- then scripts/install_app.sh into each
#   - replay-sync       scripts/process_blocks.sh over both clones at once, one with
#                       haf_fyp.processor_timings on. The plain clone syncs to 5000000
#                       and backfills its window from the stub at 5000000. The timed one
#                       syncs to 4000000 (backfilling at 4000000), its stub is extended to
#                       5000000 as hivemind's sync would, and it syncs on to 5000000,
#                       following the stub block by block. Each run's wall time, and the
#                       timed run's driver log, under test-results/<suite>/; then
#                       haf_fyp_app.restore_deferred_indexes() in each, timed: the lane
#                       stops at 5M while the context is still massive
#   - replay-parity     row counts and checksums of the history tables agree between the
#                       two clones -- so following hivemind ends where a backfill at the
#                       same block does -- each clone's history state is the one its path
#                       leaves, and each clone's indexes and constraints are the ones its
#                       install made, all valid
#   - replay-report     scripts/processor_timings_report.py over the timed run's log:
#                       processor-timings.json and its summary table
#
# fastapi to install-haf need the stack (.aidev/test-stack.compose.yml); the compose
# lanes run through .aidev/compose_lane.py. The replay steps need the shared HAF
# (sandbox.external haf, provider haf/haf), each needs the one before it, and the clones
# are dropped when the script exits, pass or fail. Reports go to test-results/<suite>/:
# junit.xml (one case per step) and the pytest/tavern junit files the jobs write.
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

# --- the replay over clones of the shared HAF -----------------------------------------
#
# The two syncs run at the same time, on two clones: one after another they would not
# fit the slot's wall. So each one's wall time is measured beside the other.
STOP_AT_BLOCK=5000000
# Where the timed clone's stub of hivemind stands when it backfills (#97); it is extended
# to STOP_AT_BLOCK between its two syncs, so the rest of the range is followed.
FOLLOW_FROM_BLOCK=4000000
# The shared HAF trusts haf_shared_consumer from the fleet LAN: no password.
CONSUMER_URL="postgresql://haf_shared_consumer@${AIDEV_EXTERNAL_HAF_HOST:-}:${AIDEV_EXTERNAL_HAF_PORT:-5432}"
declare -A clones=()
declare -A clone_ids=()
declare -A sync_seconds=()
declare -A restore_seconds=()
sync_pids=()
replay_stage=""
replay_installed=""
replay_synced=""
# The script's own stderr, which a step's redirected output does not reach: where the
# clones' names and their drops are reported.
exec 3>&2

open_clone() {
    local which="$1" run_id clone
    run_id="haf-fyp-$suite-$which-$(date -u +%Y%m%d%H%M%S)-$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"
    clone="$(psql -X "$CONSUMER_URL/haf_shared" -v ON_ERROR_STOP=on -Atc "select haf_shared.clone_run_database('$run_id')")" || return 1
    clones[$which]="$clone"
    clone_ids[$which]="$run_id"
    echo "== shared HAF clone $clone ($which)" | tee /dev/fd/3
}

drop_clones() {
    local which
    for which in "${!clones[@]}"; do
        if psql -X "$CONSUMER_URL/haf_shared" -v ON_ERROR_STOP=on -qAtc "select haf_shared.drop_run_database('${clone_ids[$which]}')"; then
            echo "== dropped shared HAF clone ${clones[$which]}" >&3
        else
            echo "== could not drop shared HAF clone ${clones[$which]}; the provider reaps it after its TTL" >&3
        fi
    done
}

cleanup() {
    local pid
    for pid in "${sync_pids[@]}"; do kill "$pid" 2>/dev/null || true; done
    drop_clones
    if [ -n "$replay_stage" ]; then rm -rf "$replay_stage"; fi
}
trap cleanup EXIT
trap 'exit 143' TERM INT

clone_q() { psql -X "$CONSUMER_URL/${clones[$1]}" -v ON_ERROR_STOP=on "${@:2}"; }

# The install script is run from a scratch copy, executable as Dockerfile.sql makes it:
# install_with_app_lock.py runs it again as its child. The shared HAF has no pgvector, and the
# consumer is no superuser and cannot create it, so on such a HAF the copy declares
# user_profile_vectors.interest_vector as real[] and skips the install's pgvector check.
# Block processing never touches that column. Each edit must hit exactly once.
stage_replay_sources() {
    local has_vector="$1"
    replay_stage="$(mktemp -d)"
    cp -r db endpoints scripts "$replay_stage/"
    chmod +x "$replay_stage"/scripts/*.sh
    [ "$has_vector" = t ] && return 0
    echo "the shared HAF has no pgvector: interest_vector is staged as real[]"
    replace_once "$replay_stage/db/create_tables.sql" 'interest_vector vector(1024)' 'interest_vector real[]' &&
        replace_once "$replay_stage/scripts/install_app.sh" "IF to_regtype('vector') IS NOT NULL THEN" 'IF TRUE THEN'
}

replace_once() {
    local file="$1" from="$2" to="$3" n
    n="$(grep -cF -- "$from" "$file")"
    [ "$n" = 1 ] || { echo "$file: expected '$from' once, found it $n times"; return 1; }
    python3 -c 'import sys; p, a, b = sys.argv[1:]; t = open(p).read(); open(p, "w").write(t.replace(a, b))' "$file" "$from" "$to"
}

# The stub of hivemind's tables for one clone, from its own operations, up to $3.
build_hivemind_stub() {
    local which="$1" first="$2" last="$3" t0
    t0="$(date +%s.%N)"
    clone_q "$which" -q -f tests/hivemind_stub.sql || return 1
    clone_q "$which" -q -At -v first_block="$first" -v last_block="$last" \
        -f tests/hivemind_stub_from_haf.sql || return 1
    echo "$which: hivemind stub $first..$last built in $(echo "$t0 $(date +%s.%N)" | awk '{ printf "%.1f", $2 - $1 }')s"
}

replay_install() {
    local which has_vector lag pids=() rc=0 pid
    if [ -z "${AIDEV_EXTERNAL_HAF_HOST:-}" ]; then
        echo "AIDEV_EXTERNAL_HAF_HOST is unset: this step needs sandbox.external haf (provider haf/haf)"
        return 1
    fi
    for which in timed plain; do open_clone "$which" || return 1; done
    has_vector="$(clone_q timed -Atc "select to_regtype('vector') is not null")" || return 1
    stage_replay_sources "$has_vector" || return 1
    # Before the install, as hivemind is on a node: the install grants haf_fyp's roles
    # what they read of it.
    build_hivemind_stub timed 1 "$FOLLOW_FROM_BLOCK" & pids+=("$!")
    build_hivemind_stub plain 1 "$STOP_AT_BLOCK" & pids+=("$!")
    for pid in "${pids[@]}"; do wait "$pid" || rc=1; done
    [ "$rc" -eq 0 ] || return 1
    for which in timed plain; do
        # The consumer owns the clone; the app's roles need to create its schemas in it.
        clone_q "$which" -c "GRANT CREATE, CONNECT ON DATABASE \"${clones[$which]}\" TO haf_fyp_owner, haf_fyp_user" || return 1
        # The timed clone's stub is a million blocks behind the HAF's head: allowed, so
        # it backfills there and then follows.
        lag=1000
        [ "$which" = timed ] && lag=$((STOP_AT_BLOCK - FOLLOW_FROM_BLOCK))
        echo "== install into ${clones[$which]}"
        (cd "$replay_stage" && POSTGRES_HOST="$AIDEV_EXTERNAL_HAF_HOST" POSTGRES_PORT="${AIDEV_EXTERNAL_HAF_PORT:-5432}" \
            POSTGRES_USER=haf_shared_consumer POSTGRES_DB="${clones[$which]}" \
            GIT_HASH="$(git rev-parse HEAD 2>/dev/null || echo aidev)" \
            FYP_HIVEMIND_MAX_LAG_BLOCKS="$lag" \
            HAF_FYP_INSTALL_LOCK_WRAPPER=/opt/psql-image/usr/local/bin/install_with_app_lock.py \
            bash scripts/install_app.sh) || return 1
        schema_shape "$which" > "$out/replay-schema-$which-installed.txt" || return 1
    done
    replay_installed=yes
}

# One run of scripts/process_blocks.sh as production runs it, connected as the consumer
# acting as haf_fyp_owner, to block $2, appending to <log>. Prints its wall time. A TERM
# (cleanup) is passed on to the driver, which stops after its current batch.
run_sync() {
    local which="$1" stop="$2" options="-c role=haf_fyp_owner" log="$out/replay-sync-$1.log"
    [ "$which" = timed ] && options="$options -c haf_fyp.processor_timings=on"
    local t0 child rc=0
    t0="$(date +%s.%N)"
    PATH="/opt/psql-image/usr/local/bin:$PATH" PGOPTIONS="$options" \
        POSTGRES_HOST="$AIDEV_EXTERNAL_HAF_HOST" POSTGRES_PORT="${AIDEV_EXTERNAL_HAF_PORT:-5432}" \
        POSTGRES_USER=haf_shared_consumer POSTGRES_DB="${clones[$which]}" \
        HAF_FYP_STARTUP_TIME_FILE="$(mktemp)" HAF_FYP_PARKED_FILE="$(mktemp -u)" \
        bash scripts/process_blocks.sh --stop-at-block="$stop" >> "$log" 2>&1 &
    child=$!
    trap 'kill -TERM "$child" 2>/dev/null' TERM
    wait "$child" || rc=$?
    while kill -0 "$child" 2>/dev/null; do wait "$child" || rc=$?; done
    echo "$t0 $(date +%s.%N)" | awk '{ printf "%.3f\n", $2 - $1 }'
    return "$rc"
}

# A clone's whole sync, in the background. Writes the wall time of its process_blocks.sh
# runs to <log>.seconds: the timed clone's two, without the stub extension between them.
start_sync() {
    local which="$1" log="$out/replay-sync-$1.log"
    : > "$log"
    (
        trap 'exit 143' TERM
        if [ "$which" = plain ]; then
            run_sync plain "$STOP_AT_BLOCK" > "$log.seconds" || exit 1
        else
            first="$(run_sync timed "$FOLLOW_FROM_BLOCK")" || exit 1
            build_hivemind_stub timed "$((FOLLOW_FROM_BLOCK + 1))" "$STOP_AT_BLOCK" >> "$log" 2>&1 || exit 1
            second="$(run_sync timed "$STOP_AT_BLOCK")" || exit 1
            echo "$first $second" | awk '{ printf "%.3f\n", $1 + $2 }' > "$log.seconds"
        fi
    ) &
    sync_pids+=("$!")
}

replay_sync() {
    local i rc=0 which block
    local order=(timed plain)
    for which in "${order[@]}"; do start_sync "$which"; done
    for i in "${!order[@]}"; do
        which="${order[$i]}"
        if wait "${sync_pids[$i]}"; then
            echo "$which sync finished in $(cat "$out/replay-sync-$which.log.seconds")s"
        else
            echo "$which sync failed; the end of its log ($out/replay-sync-$which.log):"
            tail -30 "$out/replay-sync-$which.log"
            rc=1
        fi
    done
    sync_pids=()
    [ "$rc" -eq 0 ] || return 1
    for which in "${order[@]}"; do
        block="$(clone_q "$which" -Atc "select hive.app_get_current_block_num('haf_fyp')")" || return 1
        echo "$which: processed up to block $block"
        [ "$block" -ge "$STOP_AT_BLOCK" ] || { echo "expected block $STOP_AT_BLOCK"; return 1; }
        sync_seconds[$which]="$(cat "$out/replay-sync-$which.log.seconds")"
        grep -h "haf_fyp_backfill" "$out/replay-sync-$which.log" | sed "s/^/$which: /" || true
    done
    # The sync stops at a fixed block, still in MASSIVE_PROCESSING, so the first live
    # range that would rebuild the deferred indexes never runs: rebuild them here.
    for which in "${order[@]}"; do
        restore_seconds[$which]="$(restore_deferred "$which")" || return 1
        echo "$which: deferred indexes restored in ${restore_seconds[$which]}s"
    done
    replay_synced=yes
}

restore_deferred() {
    local t0
    t0="$(date +%s.%N)"
    clone_q "$1" -qAt -c "SET ROLE haf_fyp_owner" -c "SELECT haf_fyp_app.restore_deferred_indexes()" > /dev/null || return 1
    echo "$t0 $(date +%s.%N)" | awk '{ printf "%.3f\n", $2 - $1 }'
}

# Every index and constraint of the app schema, with whether it is valid: what the
# install made has to be what is there after the sync and the restore.
schema_shape() {
    clone_q "$1" -Atc "
        SELECT 'index ' || x.indexrelid::regclass || ' ' || pg_get_indexdef(x.indexrelid) || ' valid=' || x.indisvalid
          FROM pg_index x JOIN pg_class c ON c.oid = x.indrelid JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'haf_fyp_app'
        UNION ALL
        SELECT 'constraint ' || k.conrelid::regclass || ' ' || k.conname || ' ' || pg_get_constraintdef(k.oid) || ' valid=' || k.convalidated
          FROM pg_constraint k JOIN pg_namespace n ON n.oid = k.connamespace
         WHERE n.nspname = 'haf_fyp_app'
        ORDER BY 1"
}

# Order-independent: a row count and the sum of the rows' hashes. engagement_metrics'
# updated_at is NOW() at processing time, so it is left out.
table_checksums() {
    local table sql="" row
    for table in post_candidates post_commenters engagement_metrics user_interactions \
        active_users user_communities; do
        row="t"
        [ "$table" = engagement_metrics ] &&
            row="row(t.post_id, t.comment_count, t.unique_commenters, t.reblog_count, t.vote_count, t.net_rshares)"
        [ -n "$sql" ] && sql="$sql UNION ALL "
        sql="$sql(SELECT '$table ' || count(*) || ' ' || coalesce(sum(('x' || substr(md5($row::text), 1, 15))::bit(60)::bigint::numeric), 0)
          FROM haf_fyp_app.$table t)"
    done
    clone_q "$1" -Atc "$sql"
}

# Where each clone's copy of hivemind stands: the block it backfilled at, and the block
# it is current to.
history_state() {
    clone_q "$1" -Atc "SELECT backfill_block, followed_through_block, snapshot_communities FROM haf_fyp_app.hivemind_follow"
}

replay_parity() {
    local timed plain which
    timed="$(table_checksums timed)" || return 1
    plain="$(table_checksums plain)" || return 1
    echo "backfilled at $FOLLOW_FROM_BLOCK and followed to $STOP_AT_BLOCK, instrumentation on (table rows checksum):"
    echo "$timed"
    echo "backfilled at $STOP_AT_BLOCK, instrumentation off:"; echo "$plain"
    [ "$timed" = "$plain" ] || { echo "the two clones differ"; return 1; }
    [ "$(history_state timed)" = "$FOLLOW_FROM_BLOCK|$STOP_AT_BLOCK|f" ] ||
        { echo "timed clone's history state is $(history_state timed)"; return 1; }
    [ "$(history_state plain)" = "$STOP_AT_BLOCK|$STOP_AT_BLOCK|f" ] ||
        { echo "plain clone's history state is $(history_state plain)"; return 1; }
    for which in timed plain; do
        schema_shape "$which" > "$out/replay-schema-$which-synced.txt" || return 1
        if ! diff "$out/replay-schema-$which-installed.txt" "$out/replay-schema-$which-synced.txt"; then
            echo "$which: the indexes and constraints after the sync are not the ones the install made"
            return 1
        fi
        if grep -q 'valid=false' "$out/replay-schema-$which-synced.txt"; then
            echo "$which: an index or constraint is not valid"; return 1
        fi
    done
    echo "indexes and constraints: as installed, all valid ($(wc -l < "$out/replay-schema-timed-synced.txt") of them)"
    # Two empty tables agree as well as two full ones. Communities came to the chain in
    # 2019, near block 36,000,000, so user_communities is empty this early.
    awk '$2 == 0 && $1 != "user_communities" { print "no rows in " $1; bad = 1 } END { exit bad }' <<< "$timed"
}

replay_report() {
    python3 scripts/processor_timings_report.py --log "$out/replay-sync-timed.log" \
        --out "$out/processor-timings.json" --first-block 1 --last-block "$STOP_AT_BLOCK" \
        --sync-seconds "${sync_seconds[timed]}" --uninstrumented-sync-seconds "${sync_seconds[plain]}" \
        --index-restore-seconds "${restore_seconds[timed]}" --uninstrumented-index-restore-seconds "${restore_seconds[plain]}"
}

needs() {
    local state="$1" what="$2"; shift 2
    [ -n "$state" ] || { echo "$what did not run or failed"; return 1; }
    "$@"
}

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
        # The python jobs run in /opt/python-tests here, not in the base image's /python_venv
        # as in CI, so their pytest/tavern pins are read from the latter
        # (scripts/python_test_tools_pins.py).
        unit) step unit job python-tests --coverage-xml "$out/coverage.xml" --env "COVERAGE_FILE=$PWD/$out/.coverage" \
                --env PYTHON_TEST_TOOLS_PYTHON=/python_venv/bin/python3 ;;
        fastapi) step fastapi job fastapi-tests --env "POSTGRES_HOST=${HAF_FYP_TEST_FASTAPI_DB:-fastapi-db}" \
                --env PYTHON_TEST_TOOLS_PYTHON=/python_venv/bin/python3 ;;
        # The tavern container's image is the runtime's base: its /python_venv, untouched.
        postgrest)
            step postgrest job postgrest-tests --compose-lane --path /python_venv/bin \
                --env "PYTEST_ADDOPTS=--junitxml=$PWD/$out/postgrest-tavern.xml" ;;
        install) step install job install-test --compose-lane ;;
        install-haf) step install-haf job install-test-haf --compose-lane ;;
        replay-install) step replay-install replay_install ;;
        replay-sync) step replay-sync needs "$replay_installed" replay-install replay_sync ;;
        replay-parity) step replay-parity needs "$replay_synced" replay-sync replay_parity ;;
        replay-report) step replay-report needs "$replay_synced" replay-sync replay_report ;;
        *) echo "unknown step: $s" >&2; exit 2 ;;
    esac
done
python3 .aidev/junit_cases.py "$out/junit.xml" "$suite" "$cases"
exit "$status"
