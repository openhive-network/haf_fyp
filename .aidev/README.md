# haf_fyp under AIDEV

AIDEV verifies changes to this repository through the slots in `project.yaml`,
integrates them into `aidev/integration`, and hands that off to `develop` by merge
request (`branches.handoff`, `aidev handoff`), gated by this project's GitLab CI. Merge
those merge requests **keeping the source branch**: `aidev/integration` is protected, and
AIDEV's running work is based on it.

GitLab CI runs no pipeline for AIDEV's branches: every lane carries `.standard-rules`,
which admit merge requests and `develop` only, so a push to `ai/*`, `session/*` or
`aidev/integration` creates none. The one AIDEV rule in `.standard-rules` admits a
pipeline started from the API or the web UI on `aidev/integration` — `aidev handoff`
starts one for the merge request it opens, and GitLab refuses a pipeline no job is in
(`tests/unit/test_install_lane.py` states the rules).

## Suites

`.aidev/run-checks.sh <suite> <step>...` runs the named steps and writes
`test-results/<suite>/junit.xml`, one test case per step, with the step's log tail as the
failure body, plus the junit of every pytest run inside a step (one case per test).

Every step except `stack-pins`, `aidev-scripts` and the `replay-*` steps **is a job of `.gitlab-ci.yml`**, run
from that file by `.aidev/ci_job.py`: its `before_script`/`script`/`after_script` under the
runner's `set -eo pipefail`, its `variables:`, and `CI=true`, so the tests that fail rather
than skip in CI do so here too. Change a job and AIDEV runs the changed job. What
`ci_job.py` does differently, and why, is in its docstring; in short — the job's
`services:` come from the test stack, and its `apt-get` / `apk` / `pip install` lines are
not run, because the runtime image has the tools: each skipped `pip install` is checked
instead, and a pin the image does not carry fails the step (rebuild the image, below).

| Step | CI job | What |
|---|---|---|
| `stack-pins` | — | the stack's images and the runtime image's tools are what CI runs (`.aidev/stack_pins.py`) |
| `aidev-scripts` | — | ShellCheck and a byte-compile over `.aidev/`'s own scripts |
| `sql-lint`, `shell-lint`, `python-lint`, `hadolint`, `compose-validate` | the same | verbatim |
| `openapi-drift` | `openapi-drift` | in a scratch copy of the tree (it regenerates `endpoints/`), offline: the image carries `process_openapi.py` at the job's `COMMON_CI_REF` |
| `unit` | `python-tests` | `pytest tests/unit` with its coverage floor; `coverage.xml` beside the junit; pytest/tavern pins read offline from the base image (`PYTHON_TEST_TOOLS_PYTHON`) |
| `fastapi` | `fastapi-tests` | against the stack's `fastapi-db`: `tests/integration`, then uvicorn and the FastAPI tavern file; pins as `unit` |
| `postgrest` | `postgrest-tests` | `docker/docker-compose-ci.yml`'s `setup` and `tavern` against the stack's database, PostgREST and rewriter; tavern from the base image's `/python_venv` |
| `install` | `install-test` | `docker/docker-compose-ci-install.yml`: install, install under a held lock, reinstall, uninstall, each verified, over the model of HAF |
| `install-haf` | `install-test-haf` | the same chain over the stack's HAF |
| `replay-install` | — | two copy-on-write clones of the shared 5M-block HAF, a stub of the hivemind tables haf_fyp reads built in each from its own operations (`tests/hivemind_stub_from_haf.sql`), `scripts/install_app.sh` into each as `haf_shared_consumer` |
| `replay-sync` | — | `scripts/process_blocks.sh` over both clones at once, one with `haf_fyp.processor_timings` on: the plain one backfills from its stub at 5,000,000, the timed one backfills at 4,000,000 and follows its stub, extended, to 5,000,000; then `haf_fyp_app.restore_deferred_indexes()` in each, timed |
| `replay-parity` | — | the history tables (`post_candidates`, `post_commenters`, `engagement_metrics`, `user_interactions`, `active_users`, `user_communities`) hold the same rows in both clones, each clone's history state is the one its path leaves, and each clone's indexes and constraints are the ones its install made, all valid |
| `replay-report` | — | `scripts/processor_timings_report.py` over the timed run's driver log: `processor-timings.json` and a summary table |

| Slot | Steps | Stack |
|---|---|---|
| quick | stack-pins, aidev-scripts, the six lint jobs, unit | no |
| coverage | unit | no |
| full, canary | quick's plus fastapi, postgrest, install, install-haf, and the four replay steps | yes, and the shared HAF |
| baseline | unit, fastapi, postgrest, install, install-haf | yes |

`static` and `system` are unbound: AIDEV runs neither for a project. Not run by AIDEV:
`haf-shim-drift` (it fetches `hive/haf` over the network; the unit tests check the
vendored copy offline), the `docker-build-*` jobs' image smoke tests (they need a docker
daemon), and the scheduled `registry-resolution` / `haf-shim-drift-develop`. Only the
replay steps need chain data, and they take it from the shared HAF (below); every other
lane builds its own database.

### The replay over the shared HAF

`full` and `canary` reach the shared HAF the `haf` project hosts, a HAF replayed to block
5,000,000 (`sandbox.external`, provider `haf/haf`), as `AIDEV_EXTERNAL_HAF_HOST`/`_PORT`.
`replay-install` takes two clones of it with `haf_shared.clone_run_database`, grants each
to `haf_fyp_owner` and `haf_fyp_user`, and installs the candidate into each as
`haf_shared_consumer`, under HAF's install lock. That HAF provides haf_fyp's two roles, and
the consumer may change none: `db/builtin_roles.sql` leaves a role set that is already
right alone. It has no pgvector either, which the consumer cannot create, so the install
runs from a scratch copy that declares `interest_vector` as `real[]` (block processing
never touches it); the copy is the tree unchanged once the HAF has pgvector.

haf_fyp copies its history from hivemind (#97), and the shared HAF has no hivemind, so
`replay-install` first builds, in each clone, a stub of the hivemind tables haf_fyp reads
(`tests/hivemind_stub.sql`), filled from the clone's own operations by
`tests/hivemind_stub_from_haf.sql`: posts, votes and reblogs as hivemind keeps them, ids
in chain order, so a stub built in two ranges is the stub built in one. About two minutes
for 5,000,000 blocks. The timed clone's stub stops at block 4,000,000, the plain clone's
at 5,000,000; the timed clone is installed with `FYP_HIVEMIND_MAX_LAG_BLOCKS=1000000`, so
a stub a million blocks behind the HAF's head counts as live.

`replay-sync` runs `scripts/process_blocks.sh` over both clones at the same time,
connected as the consumer with `role=haf_fyp_owner`: one after the other they would not
fit the slot's wall. The plain clone syncs to block 5,000,000 and backfills its window
from the stub, at 5,000,000, in its first batch. The timed clone syncs to 4,000,000,
backfilling at 4,000,000; its stub is then extended to 5,000,000, as hivemind's own sync
would, and it syncs on to 5,000,000, following the stub batch by batch. It has
`haf_fyp.processor_timings=on` (`db/process_block_range.sql`), so its driver log,
`replay-sync-timed.log`, holds every processor's time for every batch and the backfill's
own line. The sync stops at block 5,000,000 with the context still in
`MASSIVE_PROCESSING`, so the indexes HAF dropped for it
(`haf_fyp_app.deferred_during_massive_sync()`) are still dropped: `replay-sync` then
rebuilds them in each clone with `haf_fyp_app.restore_deferred_indexes()` and times that.
`replay-parity` checks that both left the same rows -- so following hivemind ends where a
backfill at the same block does -- that each clone's history state is the one its path
leaves, and that each clone's indexes and constraints are the ones its install made, all
valid. `replay-report` turns the timed log into `processor-timings.json` (per-processor
time in total and per 1M blocks, rows read per op type, the backfill, both runs' sync and
restore wall times and their sums) and prints its table. The clones are dropped when the
script exits, pass or fail.

### The compose lanes

`postgrest-tests`, `install-test` and `install-test-haf` drive a `docker/` compose file
with `docker compose up / wait / logs / down` in DinD. The runtime container has no
docker daemon, so `ci_job.py --compose-lane` puts `.aidev/compose_lane.py` first on
`PATH` as `docker`, and the job's script runs unchanged against it:

- the lane's long-running servers are services of the test stack, named per compose file
  in `.aidev/compose-lanes.yaml` (`external:`); a lane name the stack spells differently
  (`postgres` → `install-db`) is rewritten where the lane addresses it;
- every other service runs in the runtime as a process, in `depends_on` order, honouring
  its conditions and healthchecks; bind mounts are rewritten to the checkout paths they
  mount; a service built from `Dockerfile.sql` runs its entrypoint over a copy of what
  that Dockerfile's `COPY` lines put into the image;
- in CI PostgREST starts after `setup`; the stack's starts before it, so the lane tells
  it to reload its schema cache once `setup` has run (`after:`).

## The test stack (`test-stack.compose.yml`)

| Service | For | Image |
|---|---|---|
| `fastapi-db` | `fastapi-tests` | `postgres:14-alpine` (the job's `services:`) |
| `postgres`, `haf-fyp-postgrest`, `haf-fyp-postgrest-rewriter` | `postgrest-tests` | `pgvector/pgvector:pg14`, `postgrest/postgrest:v12.2.3`, and the rewriter built from the candidate's `Dockerfile.rewriter` |
| `install-db` | `install-test` | `pgvector/pgvector:pg18` |
| `haf-init`, `haf` | `install-test-haf` | `hive/haf:${HAF_INSTALL_LANE_VERSION}` from `docker/.env` (1.28.8), initialised with `--exit-before-sync`, served with `--skip-hived` |

Where it can, a service `extends` the lane's own definition; every image is pinned by
digest. When a lane's image moves (a tag in a `docker/` compose file, `docker/.env`'s
`HAF_INSTALL_LANE_VERSION`, the `fastapi-tests` service), re-pin the stack service to the
new tag's digest (`docker buildx imagetools inspect <image:tag>`); `stack-pins` fails until
it matches.

To run the checks by hand:

```bash
docker compose -p fyp-checks -f .aidev/test-stack.compose.yml up -d --wait --build
image="$(grep -o 'registry[^"]*aidev-tests@sha256:[0-9a-f]*' .aidev/project.yaml)"
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/w" -w /w --network fyp-checks_default "$image" \
    .aidev/run-checks.sh full stack-pins aidev-scripts sql-lint shell-lint python-lint hadolint \
    compose-validate openapi-drift unit fastapi postgrest install install-haf
docker compose -p fyp-checks -f .aidev/test-stack.compose.yml down -v
```

Bring the stack up fresh for each run: the lanes expect empty databases.

## The test runtime image (`runtime/`)

The checks run in a container with your uid, built from `runtime/Dockerfile` and
`requirements.txt` (`environment.inputs`) on common-ci-configuration's `python-test-tools`
(`.gitlab-ci.yml`'s `PYTHON_TEST_TOOLS_IMAGE`, the tavern container's image), whose
`/python_venv` is left as it is, with git, curl, jq and psql 18 from it. The python jobs
run in that image in CI, so its Python 3.14 is theirs, and beside `/python_venv` it carries
the python jobs' environment in `/opt/python-tests`, first on PATH: `requirements.txt` and
`pytest`/`pytest-cov`/`httpx`/`pyyaml`/`tavern`, pytest and tavern at `/python_venv`'s
versions, as CI holds what it installs to them; the lint tools in `/opt/lint`
(requirements.txt deliberately not there, as in `python-lint`); and the openapi generator
and validator in `/opt/openapi`. Then ShellCheck 0.11.0, hadolint 2.12.0, the docker CLI
with compose (for `config` only), PostgREST 12.2.3, and `install_with_app_lock.py` and
`haf_app_driver.py` from `Dockerfile.sql`'s base image. Every base is pinned by digest.

When `requirements.txt`, a CI tool pin or `runtime/Dockerfile` changes, rebuild and
re-pin in the same commit. CI follows `python-test-tools:develop`, and the runtime the
digest it was built from, so when common-ci-configuration moves a tool, re-pin
`runtime/Dockerfile`'s base to the tag's new digest and rebuild: `stack-pins` runs offline
and cannot see that move.

```bash
.aidev/runtime/build.sh --push   # prints registry.gitlab.syncad.com/hive/haf_fyp/aidev-tests@sha256:<digest>
```

and put the printed reference into `environment.image` in `project.yaml`. The tag is the
hash of the two inputs; an existing tag is reused, not rebuilt. Pushing needs push access
to this project's registry.
