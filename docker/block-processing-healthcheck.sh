#! /bin/sh
# Docker healthcheck for the haf_fyp block-processing container (#27).
#
# Modelled on balance_tracker/docker/scripts/block-processing-healthcheck.sh and
# haf_block_explorer's, which are the shipped reference implementations. Parts that
# look incidental are not, and are kept deliberately:
#
#   - No 2>&1 anywhere. The verdict is a single 't'/'f' on stdout; psql's stderr goes
#     to the container log where an operator can read it. Folding stderr into the
#     compared value turns any diagnostic byte into a permanent verdict.
#   - `exec [ "$(...)" = t ]` as the last statement. If psql fails the substitution is
#     empty, the comparison is false, and the check fails CLOSED. No path through this
#     script reports healthy without the database having said 't'.
#   - LC_ALL=C, because a container usually has no locale and psql warns on stderr
#     when it cannot set one.
#   - The trap kills a psql still waiting on the server when Docker times the check
#     out, instead of leaving a backend behind per probe.
#
# WHAT IT REPLACES. The compose healthcheck was `psql ... -c 'SELECT 1'`, which proves
# PostgreSQL answers and says nothing about the container's job. haf_fyp_app.main() is
# an infinite loop; when it wedges the process stays up, psql keeps answering, and the
# feed silently goes stale.
#
# THE SIGNAL is hafd.contexts.last_active_at, a HAF CORE column updated by
# hive.app_next_iteration (haf:application_loop/loop.sql).
#
# BE PRECISE ABOUT WHAT IT IS, because two earlier revisions of this file were not.
#
# It is written once per iteration, and it is written with a clock that is ALREADY ONE
# ITERATION OLD: `__now TIMESTAMP := NOW()` is a DECLARE initializer, evaluated at
# procedure entry, and NOW() is transaction_timestamp() -- the start of the caller's
# still-open transaction, i.e. the one that just did the PREVIOUS batch. The COMMIT
# comes after. HAF's own SLOW_PROCESSING alarm computes __now - __previous_active_at_time
# and calls it "the time since the previous call", which only makes sense this way.
#
# Two consequences:
#   - the value committed at the end of iteration k carries iteration k-1's start, so
#     the age this check observes spans up to TWO iteration durations. A 300s window
#     therefore tolerates iterations of about 150s, not 300s.
#   - it is NOT a sub-batch heartbeat, and it is not "frozen the same way
#     current_block_num is" either: the block pointer is written with the real end of
#     the batch, this with a clock one batch behind.
#
# What it buys over the block pointer is the OTHER case: at the chain tip the loop
# keeps calling app_next_iteration while waiting for a block, so the heartbeat ticks
# even though no block was processed. That distinguishes "idle, waiting for the chain"
# from "wedged", which a block pointer cannot.
#
# But be honest about the converse, which an earlier revision of this comment was not.
# This proves the loop is ITERATING, not ADVANCING. The heartbeat UPDATE and its COMMIT
# sit BEFORE EVERY early return in app_next_iteration -- the app_is_paused and
# NOT is_instance_ready branches both pg_sleep and RETURN afterwards -- so with hived
# down, or the app paused by hive.app_pause, the loop spins twice a second writing a
# fresh heartbeat and this check reports HEALTHY while zero blocks are processed.
#
# Chain staleness is a different quantity and belongs to the HAProxy agent-check
# (hive.get_app_current_block_age), tracked in #29. The two are complementary; this one
# does not subsume it, and nothing here is a reason to drop it.
#
# The consequence, stated rather than hidden: during a long MASSIVE batch this check
# CAN report unhealthy on a perfectly healthy replay. See MAX_AGE.
#
# Healthy if EITHER:
#   - the loop iterated within MAX_AGE, and since this container started; or
#   - hive.is_app_in_sync(), i.e. the app has caught up to whatever HAF has.
#
# The second arm is upstream's and is kept for parity, but be clear about its cost:
# it is OR-ed, so it can report healthy without the freshness test passing at all.
# Right after a restart of an in-sync app that means healthy before the new process
# has completed an iteration -- the startup-time guard is bypassed in one of the cases
# it was written for. And if hived stops AND main() wedges, current_block_num and
# consistent_block freeze together, so is_app_in_sync stays true indefinitely.
#
# Do NOT reach for "a stopped hived is caught by the haf container's own healthcheck":
# that healthcheck is `pg_isready` (deployment/docker-compose.yml), a PostgreSQL
# liveness probe. hived can be dead with postgres answering happily. An earlier
# revision of this comment claimed that compensating control; it does not exist. The
# arm is kept for upstream parity, with the hole documented.

set -e

# Kill a pending healthcheck SQL query if the script is interrupted (Docker timeout).
trap 'trap - 2 15 && kill -- -$$' 2 15

# Whatever the container runs as. In practice that is haf_fyp_owner, because
# docker/docker-compose.yml sets POSTGRES_USER on haf-fyp-sync -- the haf_admin below
# is only a fallback for running this by hand. (balance_tracker is the same: its
# compose sets btracker_owner on the service carrying its healthcheck.)
#
# haf_fyp_owner works because db/builtin_roles.sql puts it in
# hive_applications_owner_group -> hive_applications_group, which holds both the
# hafd.contexts RLS SELECT policy and EXECUTE on hive.*. haf_fyp_user does NOT --
# builtin_roles.sql revokes that group from it deliberately -- so a deployment that
# points this at haf_fyp_user gets an empty result and a permanently unhealthy
# container with no error. The old `psql -U haf_fyp_user -c 'SELECT 1'` check did
# exactly that, so this is a live foot-gun, not a hypothetical one.
postgres_user=${POSTGRES_USER:-"haf_admin"}
postgres_host=${POSTGRES_HOST:-"haf"}
postgres_port=${POSTGRES_PORT:-5432}
postgres_db=${POSTGRES_DB:-"haf_block_log"}
# NO ${POSTGRES_URL:-...} FALLBACK. `:-` substitutes the WHOLE string, so setting
# POSTGRES_URL replaced the ?application_name= tag along with the rest -- and the
# uninstall session guard excludes this probe by exactly that literal
# (`coalesce(application_name, '') <> 'haf_fyp_health_check'`). With the tag gone the
# 60-second probe counts itself, and the teardown intermittently refuses with
# "N session(s) are still connected" on a race the operator cannot reason about.
#
# Nothing else in this app honours POSTGRES_URL either: --postgres-url is
# recognised and refused by all three scripts, because honouring it moved only the
# psql connection while every message and the teardown's confirmation prompt still
# read POSTGRES_HOST/POSTGRES_DB. One connection path, one description of it.
POSTGRES_ACCESS="postgresql://$postgres_user@$postgres_host:$postgres_port/$postgres_db?application_name=haf_fyp_health_check"

CONTEXT=${HAF_FYP_CONTEXT:-"haf_fyp"}

# Seconds the loop may go without iterating before this reports unhealthy.
#
# Upstream hard-codes 1 minute. This is 5 minutes and configurable, and the honest
# reason is that the bound is NOT KNOWN. In live sync the gap is small -- one block
# per iteration, and app_next_block waits at most ~4s -- so 60s would be ample there.
# The open question is MASSIVE, where one iteration is up to 10,000 blocks of this
# app's post and engagement ingestion. That duration has never been measured on a live
# node, and because of the one-iteration lag described above the window has to cover
# TWO of them: 300s here tolerates iterations of roughly 150s.
#
# So this errs wide, and the trade is explicit: a window that is too generous delays
# detection of a wedged loop (to MAX_AGE plus retries x interval, about 8 minutes at
# the compose defaults), while one that is too tight reports a healthy replay as
# unhealthy. haf_fyp has already caused one false alert (!41); that is why the error
# is on this side. Narrow it once a MASSIVE batch has been timed.
#
# NOT justified by deployment/healthchecks/checks/haf_fyp.sh's 360s. An earlier
# revision claimed that; it is a category error. That check measures
# hive.get_app_current_block_age -- how old the last PROCESSED BLOCK is -- which is a
# different quantity from how long ago the loop last iterated.
MAX_AGE=${HAF_FYP_HEALTHCHECK_MAX_AGE_SECONDS:-300}

# Interpolated into SQL, so it is validated rather than trusted. Digits only.
case "${MAX_AGE}" in
    ''|*[!0-9]*)
        echo "HAF_FYP_HEALTHCHECK_MAX_AGE_SECONDS must be a whole number of seconds, got '${MAX_AGE}'" >&2
        exit 1
        ;;
esac

# Written by scripts/process_blocks.sh once the database is reachable. Absence means
# block processing has not started in this container -- a different statement from
# "the app is behind", and not healthy.
#
# The path is upstream's, and is what a compose file copied from reputation_tracker or
# balance_tracker expects. It is a variable only so the tests can point writer and
# reader at a temp directory instead of the runner's real /tmp; no deployment sets it.
STARTUP_TIME_FILE=${HAF_FYP_STARTUP_TIME_FILE:-/tmp/block_processing_startup_time.txt}

# -s, not -f. scripts/process_blocks.sh writes this via a temp file and mv so it is
# never seen half-written, but an empty file is still reachable -- an older image, or a
# hand-run that was interrupted. Treating empty as "not started" reports the true state
# instead of feeding '' into a timestamp cast and surfacing a syntax error in the log.
if [ ! -s "${STARTUP_TIME_FILE}" ]; then
    echo "file ${STARTUP_TIME_FILE} is missing or empty, which means block"
    echo "processing hasn't started yet"
    exit 1
fi
STARTUP_TIME="$(cat "${STARTUP_TIME_FILE}")"

# The predicate lives in block-processing-healthcheck.sql beside this file, passed with
# -f and -v rather than assembled here. Two reasons, both learned the hard way:
#
#   - a query built by shell interpolation can only be tested by grepping the assembled
#     string, and a substring search cannot tell `a - b` from `b - a`. Swapping those
#     operands makes the freshness difference permanently negative -- a wedged loop
#     reporting healthy forever -- and every assertion still passed. As a file, the
#     whole expression is comparable against an expected one.
#   - psql's :'var' substitution quotes and escapes, so CONTEXT and STARTUP_TIME are no
#     longer concatenated into SQL at all. The MAX_AGE validation above stays as
#     belt-and-braces rather than as the only defence.
SQL_FILE=${HAF_FYP_HEALTHCHECK_SQL:-"$(dirname "$0")/block-processing-healthcheck.sql"}

if [ ! -r "${SQL_FILE}" ]; then
    echo "healthcheck SQL not found at ${SQL_FILE}" >&2
    exit 1
fi

# The docker container probably won't have a locale set, do this to suppress the warning
export LC_ALL=C
exec [ "$(psql "$POSTGRES_ACCESS" --quiet --no-align --tuples-only \
              -v context="${CONTEXT}" \
              -v max_age="${MAX_AGE}" \
              -v startup_time="${STARTUP_TIME}" \
              --file="${SQL_FILE}")" = t ]
