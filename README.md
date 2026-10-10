# haf_fyp — Personalized Feed for Hive

A HAF (Hive Application Framework) application that delivers a personalized "For You Page" feed for the Hive blockchain. Each user receives a ranked feed shaped by their on-chain engagement history — votes, reblogs, and community subscriptions — scored across four signals: semantic relevance, recency, engagement quality, and author credibility.

## How It Works

```
hived ──► sql_serializer ──► HAF PostgreSQL
                                   │
                         haf_fyp_app schema
                         ┌──────────────────┐
                         │ post_candidates  │  ◄── process_posts.sql
                         │ engagement_metrics│  ◄── process_engagement.sql
                         │ user_communities  │  ◄── process_communities.sql
                         └──────────────────┘
                                   │
                          Python ranker (5 min)
                          ┌────────────────────┐
                          │ relevance  × 0.40  │  (HiveSense embeddings)
                          │ recency    × 0.30  │  (exponential decay)
                          │ engagement × 0.20  │  (log-normalised)
                          │ credibility× 0.10  │  (btracker + reputation)
                          │ community boost×1.2│  (on-chain subscriptions)
                          └────────────────────┘
                                   │
                             feed_cache
                                   │
         rewriter (port 3001) -> PostgREST  +  FastAPI (port 3002)
```

## Stack

| Component | Role |
|-----------|------|
| HAF PostgreSQL | Fork-safe block storage and app context |
| `haf_fyp_app` schema | All application tables |
| `haf_fyp_endpoints` schema | PostgREST-exposed read functions |
| `process_blocks.sh` → `haf_fyp_app.main()` | PL/pgSQL block sync loop |
| `haf_fyp.ranker_daemon` | Python scoring and feed cache writer |
| `haf_fyp.api` | FastAPI overlay (rate limiting, interests, explainer, admin) |
| PostgREST | Auto-generated REST API |
| Redis (`haf-fyp-redis`) | Pub/sub for on-demand feed refresh, and rate-limit storage. Must not evict — see [Operating the rate limits](#operating-the-rate-limits) |

## Prerequisites

### Required

- A running HAF node (`haf_block_log` PostgreSQL database). The standalone stack starts
  one for you, from `registry.gitlab.syncad.com/hive/haf` — the repository haf publishes
  the instance image to, and the same name `haf_api_node` reads (under whichever registry
  it is pointed at; its own default is `registry.hive.blog`). The `haf/instance`
  repository beside it stopped at 1.27.x
  ([#56](https://gitlab.syncad.com/hive/haf_fyp/-/issues/56)). `docker/.env` pins
  **1.29.0-rc2**, the oldest release carrying the HAF application registry
  (`hive.app_register`, haf#341) that the block-processing driver registers with. An
  older HAF is supported and not an error: installation warns, skips registration, and
  block processing keeps to the legacy `CALL main()` loop.

  > **Pin `HAF_VERSION` before the first sync — changing it afterwards means a replay.**
  > HAF 1.29.0 added `hafd.transactions.rc_cost` and ships no migration for it on purpose:
  > the values cannot be backfilled, so HAF's update script compares schema hashes and
  > exits 1 rather than migrate. The container's `restart: unless-stopped` turns that into
  > a crash loop, and because the HAF healthcheck allows 72 h to start, nothing reports
  > unhealthy — every dependent service simply waits. To move an already-synced stack to a
  > new HAF line: stop it, move the HAF data volume aside, and let the new version replay
  > into a fresh one. (Moving a stack *between projects* keeping its data is a different
  > job — that is `scripts/upgrade_standalone_stack.sh`, and it does not change the image.)
  >
  > A stack that already runs can only be on a `1.27.x` pin, because no newer tag ever
  > resolved on the old path. Those tags exist on the new path as a **different build** of
  > the same version (`1.27.11rc3`: 277 MB here against 1.65 GB there). The schema is that
  > version's either way, so nothing replays — but the runtime underneath changes, so
  > recreate `haf` deliberately rather than discovering it on the next unrelated `up`.
- `pgvector` extension installed on the HAF PostgreSQL server:
  ```sql
  -- run as superuser before installing haf_fyp
  CREATE EXTENSION IF NOT EXISTS vector;
  ```
- Docker, and Docker Compose **v2.18.1 or newer** (older releases cannot parse the
  volume names in `docker/docker-compose.yml`, and fail every command with
  *invalid interpolation format*). From **v2.27.1** a `-p` also decides the volume
  names; before it, a `COMPOSE_PROJECT_NAME` left in the shell decides them instead.
- **[Hivemind](https://gitlab.syncad.com/hive/hivemind), installed and synced on the
  same PostgreSQL server.** This is a hard dependency, not an accuracy enhancement:

  > **Without Hivemind, every feed returns `[]`.**

  The feed endpoints render each post through
  `hivemind_app.get_full_post_view_by_id` and select only rows where
  `post_candidates.hive_post_id IS NOT NULL`. That column is filled by
  `haf_fyp_app.resolve_hivemind_post_ids`, which returns immediately when
  `hivemind_app.hive_posts` is absent, so on a node without Hivemind it stays NULL
  for every post and both `/v1/fyp/feed` and `/v1/fyp/global` return an empty list.
  Block processing continues normally and nothing raises — the resolver is
  deliberately exception-guarded so a missing schema cannot stall the processor —
  so the only symptom is empty feeds.

  Hivemind is also the bridge to HiveSense: embeddings are keyed by hivemind post
  id, so without Hivemind there is no relevance signal either.

  **If you install Hivemind AFTER haf_fyp has been syncing, you must run the
  backfill**, or the feeds stay empty even though everything now looks correct:

  ```sql
  CALL haf_fyp_app.run_hive_post_id_backfill();              -- 1-month chunks
  CALL haf_fyp_app.run_hive_post_id_backfill('1 week', 0.2); -- finer + throttled
  ```

  The per-block resolver only looks back **7 days** (`process_block_range.sql`), so
  every `post_candidates` row older than that keeps `hive_post_id IS NULL` forever
  and is filtered out of both feeds. This is the most likely way to hit the empty-feed
  failure in practice. **`GET /metrics` cannot currently detect this**, and that is worth stating rather
  than leaving to be discovered. A request-time probe for it turns out not to be
  affordable: the cheap version (unresolved and older than the window) counts posts
  *deleted on-chain*, which never resolve by design — on a healthy caught-up node
  507 of 507 sampled unresolved rows were deletions — so it would report a permanent
  false alarm with a remedy that cannot clear it. The correct predicate has to join
  hivemind to exclude them, and measured **1.9 s** on the reference node, getting
  slower as the backlog grows. Detecting it properly needs a precomputed rollup on
  the ranker's maintenance pass, the same pattern `embedding_coverage_daily` uses;
  [#43](https://gitlab.syncad.com/hive/haf_fyp/-/issues/43) stays open for it. Every privilege state
  reads `readable` throughout, which is why the earlier privilege-only check could
  not see it. The backfill is resumable and only ever touches NULL rows, so
  re-running it is safe.

### External HAF Apps (optional)

Each of these is genuinely optional — haf_fyp installs, syncs and serves without
them. What each one costs you is stated exactly; "degrades gracefully" is not a
claim any of these rows makes on its own.

| App | Provides | Without it |
|-----|----------|------------|
| [HiveSense](https://gitlab.syncad.com/hive/hivesense) | 1024-dim post embeddings (`hivesense_app.posts_vectors`, halfvec; keyed by hivemind post id) | **no personalization at all** — see below |
| [reputation_tracker](https://gitlab.syncad.com/hive/reputation_tracker) | Per-account reputation scores (`reptracker_app.account_reputations`) | credibility from VESTS alone, renormalised |
| [balance_tracker](https://gitlab.syncad.com/hive/balance_tracker) | Per-account VESTS stake (`btracker_app.current_account_balances`, or `btracker_backend` / `hafbe_bal` under the block explorer) | credibility from reputation alone, renormalised |

#### Without HiveSense there is no personalization, not "neutral relevance"

An earlier version of this README said relevance falls back to a neutral 0.5. That
is wrong about the mechanism and wrong about the outcome.

**The mechanism.** With no embeddings, `compute_scores` takes its cold-start branch
and sets the **relevance weight to 0.0**, renormalising recency/engagement/
credibility to sum to 1.0. Relevance is dropped from the sum rather than
contributing a constant.

**The outcome.** Every user is ranked on the remaining three signals and gets a real
`feed_cache` row, with the ×1.2 subscribed / ×1.1 engaged community boost applied as
usual. `fyp.source` reads `personalized`, because it is — just not semantically.

Until #41 that was not what happened. `build_user_profile_vector` and
`bootstrap_vector_from_communities` called HiveSense directly and unguarded, so any
user with recent interaction history or a community raised inside the per-user
ranking; the ranker rolled back and wrote **no row at all** for them, and the
endpoint served them the global feed *without* the boost. All four call sites now
share one guard, so an unavailable HiveSense degrades to the cold-start branch the
design always intended.

So a node without HiveSense serves a working "For You" page ordered by recency,
engagement quality and author credibility, with community boosting intact. What it
does not do is order by what a user actually reads — two users with the same
communities get the same feed.

**What you will see in the logs.** Not much, deliberately. An earlier version of
this app wrote a full traceback per affected user per ranking cycle — up to
`MAX_USERS_PER_CYCLE` (default 200) every `CYCLE_SECONDS` (300), indefinitely, which
buried real incidents. Failures are now throttled per *kind*: the first occurrence,
each new kind, and one line per hour while a condition persists, with a "recovered"
line when a cycle completes cleanly. The embedding fetch throttles separately per
call site, so a failing bulk fetch and a failing per-user fetch are two conditions
rather than one flapping between them. `run_cycle` and the force-refresh path each
report a failure count regardless, so a throttled condition is never invisible.

HiveSense needs a GPU to compute embeddings at a useful rate, so "just run HiveSense
as well" is a real operational cost rather than a checkbox. Running haf_fyp without
it is a legitimate choice — just make it deliberately.

#### Credibility sources

With NEITHER credibility source readable, every author scores the 0.3 default. A
missing component is dropped from the weighted average rather than scored 0 at full
weight (#8) — zeroing it compressed every author's credibility into `[0, 0.6]` and
shifted the ranking, so a missing tracker changed the ORDER of the feed rather than
just widening it.

#### Checking what this node actually has

`GET /metrics` classifies each probed relation, under `credibility_sources` (#8) and
`ranking_sources` (#36), so the answer does not depend on reading this table and
hoping. The distinction between the first two states matters: they used to be
indistinguishable, so a lost grant silently degraded ranking quality with nothing to
notice.

Per-relation states below. `readable_unverified` is emitted only by
`ranking_sources`; `credibility_sources` resolves a readable view to plain
`readable`, and its own `state` field has a different vocabulary again
(`full` / `reputation_only` / `vests_only` / `none`).

| state | meaning |
|-------|---------|
| `readable` | fine |
| `unreadable` | the relation exists but this deployment has lost `SELECT` on it — repairable, re-run `scripts/grant_cross_schema.sh` |
| `absent` | the app is not installed here. For an optional source that is not a fault; for hivemind it means empty feeds |
| `readable_unverified` | readable, but it is a view or a foreign table, where the privilege answer is not the whole story — `has_table_privilege` on a view stays true after the view's owner loses access to what it reads, and on a foreign table it is a purely local check. Treated as healthy, flagged because it cannot be verified. `ranking_sources` only |
| `unknown` | the probe itself failed. **Never** means healthy — every boolean in that section fails safe |

**Alert on `ranking_sources.degraded`.** Not on any single field below.

That is a correction with some history behind it. #36 shipped a `feeds_empty`
boolean as the alert field and #43 tried to make it authoritative; review showed it
was wrong in *both* directions, so it is gone rather than left to mislead:

* it read `true` on nodes that were **serving** — both feed endpoints answer from
  `post_bridge_cache`, so revoking `SELECT` on `hive_posts` on a warm node stops
  resolution while every request keeps succeeding;
* it read `false` on nodes that were **not** — `global_feed_cache` is the
  recency-weighted top 200, so the backfill case leaves it green while older
  per-user rows are unresolvable.

So the section reports two independent observations and lets `degraded` combine
them, instead of collapsing them into one boolean that is wrong at both ends:

| field | question it answers |
|-------|--------------------|
| `global_feed` | does the global cache hold anything (`cached`), and is any of it servable (`servable`)? `cached: true, servable: false` is a failure no privilege probe can see |
| `sources` | per-relation privilege states — the diagnosis of *why*, not the detection |

`global_feed` reading `null` means that probe could not run; `degraded` is set and `state` is `ranking_unknown`, because "I could not check"
must never read as fine. `sources` is never null — when the catalog probe fails
every relation reads `unknown` instead.

`state` summarises, and the values want different responses:

| state | meaning | do |
|-------|---------|----|
| `ranking_full` | nothing standing between this node and a served feed | nothing |
| `ranking_no_feed` | hivemind absent or unreadable, a resolver input unreadable, the global cache holds nothing servable, or the ranker has not run yet | `detail` distinguishes these; only the last is expected, and it is the one that does not set `degraded` by itself |
| `ranking_no_personalization` | hivesense absent or unreadable | optional — see above. `absent` is not a fault; `unreadable` is. Only reported when nothing more serious applies: a hivesense fault on a node whose global cache is also unservable shows up in `sources`, not in `state` |
| `ranking_unknown` | a probe could not run | investigate; `detail` names which one. Never means healthy |

A lost grant on `hive_accounts` or `hive_permlink_data` stops resolution just as
dead as one on `hive_posts`, and #36 shipped with both invisible — an operator saw
`hivemind: readable` while nothing resolved. They are now probed and reported under
`sources.resolver_inputs`, with their own `state` and a `detail` naming the grant to
repair. That case matters more than it looks: `get_personalized_feed` reads
`hive_accounts` **unguarded**, so an authenticated feed request *raises* rather than
returning `[]`.

The per-relation states remain as the **diagnosis** rather than the detector,
including `resolver_inputs` — the other two relations `resolve_hivemind_post_ids`
joins.

#### What this still does not catch

`global_feed` is narrower than it looks, and these cases leave feeds empty or
broken while it reads servable:

* **Per-user feeds.** The check reads the *global* feed, and `get_personalized_feed`
  does **not** fall back to the global feed for a user who already has `feed_cache`
  rows — that branch returns on its own. A user whose cached rows are all
  unresolvable gets `[]` while this reads healthy. In the backfill scenario
  specifically, the 7-day window keeps recent posts resolved, so the
  recency-weighted global cache stays servable while older per-user rows do not.
* **The render step.** A post can resolve and still be dropped: `get_bridge_post`
  returns NULL for a post deleted in hivemind, and the endpoint filters it out.
* **The hivemind views** behind `get_full_post_view_by_id`. A grant lost there never
  touches `hive_post_id`, so nothing here notices.

Answering "can every user be served" would mean probing `feed_cache` per user and
modelling the render filter. That is a larger change and is not attempted here.

To check one specific user, call `/rpc/get_user_profile` for them and compare:
**`has_feed: true` together with an empty `/rpc/get_personalized_feed` response
means their cached rows are unresolvable.** Note it has to be that pair — `fyp` is
built per feed entry, so an empty response carries no `fyp` object at all and
`fyp.source` cannot be read from it. (An earlier version of this section said to
read `fyp.source`; that state is not observable.)

There is also no staleness check: a ranker that stopped days ago leaves its last
good 200 rows in place, and they keep resolving, so `global_feed` reads servable.
**`/sync-status` does not answer this** — it reports the HAF context advanced by the
`haf-fyp-sync` service, which keeps running when `haf-fyp-ranker` dies. The signal
that does move with the ranker is the top-level `computed_at` in this same
`/metrics` payload, refreshed on the ranker's hourly maintenance pass.

After installing each external app, run:
```bash
docker compose --env-file docker/.env.local -f docker/docker-compose.yml \
  run --rm haf-fyp-setup /app/scripts/grant_cross_schema.sh
```

## Quick Start

```bash
# 1. Copy and configure environment
cp docker/.env docker/.env.local
# Edit .env.local:
#   - Set HAF_VERSION to a pinned release tag (not "latest") for production. A RELEASE
#     tag: hive/haf keeps a short-SHA tag only while it is one of the five newest or
#     younger than a week, so a commit pinned here stops resolving on the next pull,
#     on a date set by how busy haf's develop is (#56).
#   - Set FYP_ADMIN_API_KEY to a strong random secret
#   - Set TRUSTED_PROXY_IPS to whatever fronts this app (Caddy/Varnish/HAProxy),
#     or to `direct` if nothing does. Left empty, every caller shares one bucket
#     and pre-validation rate limiting stays OFF -- see the deployment checklist.
#     Listing it is not enough: X-Real-IP must also ARRIVE, set by the outermost
#     proxy and passed on by any in between.
#
# Every command below passes --env-file docker/.env.local. Without it Compose reads
# docker/.env (the project-directory default) and the edits above are silently ignored,
# leaving FYP_ADMIN_API_KEY at its shipped placeholder.

# 2. Install schema (one-time)
#    GIT_HASH is what GET /version reports. CI_COMMIT_SHORT_SHA is the first EIGHT
#    characters of the SHA and is also the image tag, so `cut -c1-8` gives the same
#    value however the node was installed. Do not substitute `git rev-parse --short=8`:
#    --short=N is a minimum, not a width -- git extends the abbreviation until it is
#    unambiguous in your clone, so it returns nine or more where that prefix collides,
#    and the hash then matches no image for its own commit (#70). Omit GIT_HASH
#    altogether and /version reports a placeholder rather than a commit.
#    --build is required: GIT_HASH is a build argument baked into the image, so a plain
#    `up` (or `run`) reuses the existing image and silently ignores a changed export.
#    It is baked rather than passed at runtime on purpose -- the image carries the SQL,
#    so the hash must describe the code inside it. See Dockerfile.sql.
export GIT_HASH="$(git rev-parse --verify HEAD | cut -c1-8)"
docker compose --env-file docker/.env.local -f docker/docker-compose.yml up --build haf-fyp-setup

# 3. Start the full stack
docker compose --env-file docker/.env.local -f docker/docker-compose.yml up \
  haf-fyp-sync haf-fyp-ranker haf-fyp-api haf-fyp-postgrest \
  haf-fyp-postgrest-rewriter haf-fyp-redis

# 4. Smoke test
curl http://localhost:3001/health
curl http://localhost:3001/v1/fyp/algorithm
#    Raw PostgREST stays on 3000 for debugging; it is NOT reachable through the
#    rewriter, whose generated catch-all rewrites /rpc/x to /rpc/rpc/x.
curl http://localhost:3000/rpc/get_health

# 5. After installing Hivemind (required) / HiveSense / btracker / reptracker
#    — grant cross-schema access
docker compose --env-file docker/.env.local -f docker/docker-compose.yml run --rm haf-fyp-setup \
  /app/scripts/grant_cross_schema.sh
```

> **Upgrading from before #38:** the published ports changed. `/rpc/...` moved from 3001
> to **3000**, and 3001 now serves the clean `/v1/fyp/...` paths through the rewriter.
> A stale `:3001/rpc/x` call returns a PostgREST 404 body rather than a connection error,
> so it reads as "the endpoint was removed" — it moved.

> **Upgrading a standalone stack from before #53** (this includes #48's rename of
> `postgrest` to `haf-fyp-postgrest`). The compose project name is now pinned to
> `haf_fyp`. Before, it defaulted to the compose file's directory, `docker`, unless you
> passed `-p` or set `COMPOSE_PROJECT_NAME`. The quick-start commands above no longer
> reach the old containers: they would create a second stack beside them, on a new, empty
> HAF volume (while the old HAF runs, the new one cannot start on port 5432 and is left
> `created`). Use the upgrade script instead.
>
> **Before you run it,** run no compose command that stops, restarts or recreates the old
> stack (`stop`, `down`, `restart`, `up`, `run`). Its containers were created before
> `stop_grace_period`, so compose gives HAF only ten seconds to shut PostgreSQL down, and
> a killed HAF runs crash recovery on its next start. For a stack that already ran under
> `haf_fyp` or a `COMPOSE_PROJECT_NAME`, that includes the quick start itself.
>
> From the repository root, with `docker/.env.local` in place:
>
> ```bash
> scripts/upgrade_standalone_stack.sh            # inspects and prints the plan; changes nothing
> scripts/upgrade_standalone_stack.sh --apply    # does it
> ```
>
> Read the plan before applying it: it names the old project, the data volume, and any
> other HAF data volume it leaves alone. The script handles a stack run from
> `docker/docker-compose.yml` alone with HAF's data in a named volume; for anything else
> it refuses, and the stack is moved by hand (below).
>
> **What it does,** with `--apply:`
> - **Finds the old project** from this checkout's HAF container that has run and is not
>   from the #53 file: one without the two-minute grace period, or any under the project
>   `docker`, which the new file pins away from. Where no such container is left, pass
>   `--old=PROJECT` naming the project whose `PROJECT_haf_datadir` holds the data; after an
>   `--apply` that failed, the `HAF_FYP_HAF_DATADIR_VOLUME` line it had already written
>   names it, and the script reads it back.
> - **Stops this checkout's other stacks:** a quick start already created from the new
>   file on another volume, or a start whose HAF never ran (one that failed on a port).
>   Their volumes are left in place.
> - **Stops the old stack's containers,** only those labelled with both the old project and
>   this checkout: the applications, then HAF, each with `docker stop -t 120`. If HAF was
>   killed at that limit it stops there (exit 3), keeping the containers so their log can
>   be read. Otherwise it removes them, and the old network if nothing else is attached.
> - **Adds `HAF_FYP_HAF_DATADIR_VOLUME`** to `docker/.env.local` when the data volume is not
>   the one the new project's name gives (`docker_haf_datadir` for the default project
>   `docker`, which moves to `haf_fyp`). A `-p` stack keeps its name, and its volume
>   already follows it.
> - **Runs `down`, then `up -d --build`** for the new project. As in the quick start, that
>   runs `haf-fyp-setup` (`install_app`) against the existing database first. It then
>   checks, twice a few seconds apart, that HAF mounts the old volume and every service
>   runs. Anything that fails after the host has changed exits 3 and says how to go on.
> - **Finishes a half-started stack:** where HAF already runs from the new file on the
>   right volume but services are down -- what an `--apply` that failed on a port leaves --
>   it starts those services instead of moving anything.
>
> **What it refuses** (exit 1), changing nothing:
> - `docker/.env.local` missing, without `HAF_VERSION`, or tracked by git (the volume line
>   it adds must not be dropped by a checkout); `HAF_FYP_HAF_DATADIR_VOLUME` or
>   `HAF_FYP_REDIS_DATA_VOLUME` set in the shell, where they outrank the env file
> - no HAF container from this checkout that has run, without `--old`; `--old` naming
>   another project than that container's
> - two HAF containers from before #53 in this checkout that have run: remove the one that
>   is not this stack's (`docker stop -t 120`, then `docker rm`) and run again
> - an old HAF that had already exited 137 before the run, unless `--accept-killed-haf`
> - an old stack started with other compose files as well, or with its data directory
>   bind-mounted
> - a data volume that does not exist or was created for another project (with `--old`),
>   or that another checkout's container or another project's also uses
> - containers of another checkout in the project the stack will run under (`down` and
>   `up` would take them over), or another directory's running containers in the old
>   project (they reach this HAF as `haf`, and would lose it)
> - a `COMPOSE_PROJECT_NAME` line other than the old stack's own project name, `POSTGREST_HOST=postgrest`
>   (renamed in #48), or a `HAF_FYP_HAF_DATADIR_VOLUME` naming another volume; or a compose
>   file that resolves another project or volume
> - when this checkout's HAF already runs from the new file: any HAF data volume on the
>   host that nothing uses (it may hold this stack's data: pass `--old` if so), or compose
>   resolving another volume than the one HAF runs on
> - `docker` unusable, an unwritable env file, a `docker compose config` that fails (which
>   is also what Compose older than v2.18.1 does here), an old HAF mounting nothing at the
>   data directory, or another run of the script already in progress
>
> It never removes a volume, and never runs `down -v` or `--remove-orphans`. A second run
> finds nothing to do, unless a HAF data volume that nothing uses is left, such as the empty
> one a quick start created. That run refuses and lists every such volume on the host: the
> list covers other checkouts' and other apps' volumes too, so remove one only if you know
> which stack created it and that nothing needs it.
>
> **Moving a stack by hand,** when the script refuses one it does not handle: stop this
> checkout's old containers with `docker stop -t 120` (HAF last), check HAF did not exit
> 137, remove them, and start the new file with the files and settings the old stack used
> plus `HAF_FYP_HAF_DATADIR_VOLUME` in `docker/.env.local` when the data volume is not
> `<the project the stack will run under>_haf_datadir` (`haf_fyp_haf_datadir` for the
> default project). Keep the stack's `-p` on every command.
>
> **Afterwards,** pass `--env-file docker/.env.local` (and the stack's `-p`, if it has one)
> to every compose command for this stack, keep the `HAF_FYP_HAF_DATADIR_VOLUME` line, and
> keep `COMPOSE_PROJECT_NAME` out of the shell: before Compose v2.27.1 one there renames
> this stack's volumes even when `-p` names the project, which starts HAF on an empty one.
> `/health` on port 3001 should report a `last_processed_block` that continues from the
> old stack's. A second checkout on the same host now shares `haf_fyp`: run it under its
> own `-p`.

## API Endpoints

All read endpoints are served through the **rewriter on port 3001**, which maps the clean paths onto PostgREST's `/rpc/` calls. That is the documented, supported surface and the one `haf_api_node` consumes. PostgREST itself is published on port 3000 for debugging. The FastAPI overlay on port 3002 handles write operations and admin actions.

### Read API (rewriter, port 3001)

These are the supported paths, and the ones `haf_api_node` consumes. The `/rpc/` column
is the PostgREST function each maps onto — it is what appears in logs and `EXPLAIN`
output, and it is reachable directly on **port 3000**, not through the rewriter (see
below).

| Method | Path (port 3001) | maps to | Description |
|--------|------------------|---------|-------------|
| GET | `/health` | `/rpc/get_health` | App status and last processed block |
| GET | `/sync-status` | `/rpc/get_sync_status` | HAF context head + its UTC timestamp — **the endpoint to monitor** (#34) |
| GET | `/version` | `/rpc/get_version` | Build version of the running app |
| GET | `/v1/fyp/algorithm` | `/rpc/get_algorithm` | Active algorithm version and weights |
| GET | `/v1/fyp/algorithms` | `/rpc/list_algorithm_versions` | All registered algorithm versions |
| GET | `/v1/fyp/feed/{username}?page-size=20` | `/rpc/get_personalized_feed` | Ranked feed for a user |
| GET | `/v1/fyp/global?page-size=20` | `/rpc/get_global_feed` | Unauthenticated global feed |
| GET | `/v1/fyp/profile/{username}` | `/rpc/get_user_profile` | User vector status and top communities |
| GET | `/v1/fyp/explain/{username}/{post_id}` | `/rpc/get_score_explanation` | Per-signal score breakdown |
| GET | `/v1/fyp/feed-metrics` | `/rpc/get_feed_metrics` | Feed performance analytics per algorithm version (#13) — product metrics, not service health |

### Raw PostgREST (port 3000, debugging)

The `/rpc/...` paths above answer directly on port 3000. They are **not** reachable through
the rewriter: its generated catch-all (`rewrite ^/(.*)$ /rpc/$1`) would turn
`/rpc/get_health` into `/rpc/rpc/get_health`. Before #38 the shipped stack published this
surface on 3001 and served no clean paths at all; it now publishes the clean paths on 3001
and this one on 3000.

Every feed entry carries a nested `fyp.source` field — `personalized` when the user has a precomputed personalized feed, or `global` when `get_personalized_feed` serves the cold-start global fallback (it is uniform across all entries in one response). A client can therefore tell directly from a feed call whether the user is getting their own ranked feed or the global fallback, without a separate `get_user_profile` lookup (`has_feed`).

### FastAPI overlay (port 3002)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Can this container serve: `503` when the database pool does not answer a query within 1.5 s. Redis is reported in the body (`redis`, `rate_limit_store`) and never fails it: the app serves without Redis. A probe slower than its deadline (1.5 s database, 1.0 s Redis) counts as no answer. Not sync-aware (#61) |
| GET | `/metrics` | Operational health: embedding coverage (#15), credibility source health (#8), ranking sources + global-feed servability (#36, #43 — alert on `ranking_sources.degraded`, `state` and `detail` say why) — not feed analytics |
| GET | `/sync-status` | Last block processed by the `haf_fyp` HAF context + its UTC timestamp (#16) |
| POST | `/v1/fyp/interests` | Declare cold-start community interests, signed (10/min per IP, 60/min total) |
| DELETE | `/v1/fyp/interests` | Un-opt-in: clear declared interests, signed (5/min per IP, 30/min total) |
| POST | `/v1/fyp/events` | Impression / engagement telemetry batch (120/min per IP, 600/min total) |
| POST | `/v1/fyp/admin/algorithm` | Activate an algorithm version, `X-Admin-Key` required (5/min per IP; 30/min total, charged only on a wrong key) |

**Interactive docs are disabled by default.** FastAPI would otherwise publish `/docs`,
`/redoc` and `/openapi.json` on this app — which is the admin surface, served under
`/haf-fyp-admin/` on a node. That would advertise a ready-made request builder for the
admin endpoint above and load Swagger UI from `cdn.jsdelivr.net`. Set
`HAF_FYP_ENABLE_DOCS=true` to get them back for local development; the value is read
once at import, so it takes a restart.

The two metrics endpoints are unrelated and answer different questions. This one, on the
FastAPI overlay, is the *operational* read — embedding coverage, credibility source
health, and ranking source health — and is what an embedding-gap, lost-grant or
empty-feed alert should watch. It carries no block
height, so it is not a freshness check: for that, watch `GET /sync-status` below. Feed
*performance* analytics are a separate, product-facing read, served by PostgREST itself as
`/rpc/get_feed_metrics` and as the clean URL `/v1/fyp/feed-metrics` through the nginx
rewriter. That clean URL was `/v1/fyp/metrics` until the two were disambiguated; the old
path no longer resolves.

`GET /sync-status` is the ecosystem-uniform freshness endpoint (same shape as
haf_block_explorer's), returning `{"last_block_num": N, "last_block_time": "YYYY-MM-DDTHH:MM:SS"}`
in UTC so a monitor computes staleness in one call. It reports HAF's authoritative
pointer, `hafd.contexts.current_block_num`, rather than the app's own
`app_status.last_processed_block` bookkeeping row that PostgREST's `/rpc/get_health`
returns. The two normally agree — `main()` advances both in the same transaction — but
they have different failure modes, and only this one carries a timestamp.
`last_block_time` is null before the first block is processed, and the endpoint returns
503 while HAF is in massive sync.

Since #34 it is served from **both** surfaces, and **the PostgREST one is what you
should monitor**: `http://<app>-postgrest-rewriter:80/sync-status (the reference stack names this service
`haf-fyp-postgrest-rewriter` to match)`, the standard probe
URL haf_api_node's per-app backends use. It exercises the real request path (rewriter
-> postgrest -> postgres), so a wedged postgrest or rewriter shows up; the FastAPI copy
on port 3002 bypasses both and cannot see either. Point `*_HEALTH_URL` at the rewriter.

The FastAPI copy stays because it is useful when the postgrest side is broken — which
is exactly when you want to ask. Both call the same `haf_fyp_app.sync_status()`, so
they cannot disagree on data; they format the timestamp in two places, so keep those
in step.

Note what 200 does **not** prove: it means reachable, and reports the context head.
A stalled app whose context sits at block 0 answers 200 with a null timestamp. For
staleness, pair it with a block-freshness agent-check.

Both interest routes are authenticated with a Hive posting-key signature over a
canonical challenge — `hivefy:interests:v1:<username>:<timestamp>:<ids>` to opt in
and `hivefy:interests:delete:v1:<username>:<timestamp>` to opt out. The two are
distinct strings on purpose, so a signature captured from one cannot be replayed
as the other inside the 300s freshness window.

`DELETE /v1/fyp/interests` removes every declared interest (all-or-nothing — POST
the remaining set to drop only some). It returns 200 with the number of rows
removed, including 0, and publishes the usual refresh signal; the ranker then
reconciles rotation membership and, for an account with no interests and no
on-chain interactions, evicts its `active_users` row and drops its cached feed and
profile vector. Accounts that have interacted on-chain stay ranked — their
membership comes from the chain, not the picker.

**The per-IP rate limit is charged before the request body is parsed (#28), so a
rejected request costs the caller its own allowance.** A malformed batch answered
`422` counts against the same per-IP bucket a successful one would, and a caller
already over the limit gets a `429` without the body being read at all. This is what
bounds total cost on `POST /v1/fyp/events`, which is unauthenticated by design:
previously a 4 MB malformed batch cost ~700 ms of CPU and returned a ~3.9 MB
validation error while charging nothing, repeatable without bound. Integrators
should note the consequence: a client with a serialisation bug now burns its own
quota rather than retrying for free.

**The shared global ceiling on each endpoint is charged later — from inside the
handler, so only requests that pass validation spend it.** The split matters. Both
limits are listed together in the table above, but they do different jobs: the
per-IP bucket bounds what one caller can make the service spend, while the ceiling
bounds *accepted* volume (for `/v1/fyp/events`, 600/min x 200 events = 7.2M rows an
hour, inside `prune_feed_events`' cap). Charging the ceiling before validation too
would let malformed traffic spread across a handful of addresses — each staying
inside its own per-IP allowance, so never throttled — drain the shared bucket and
`429` every other caller, which is a denial of service the per-IP design exists to
prevent. The admin route goes one step further and charges its ceiling only when
the key is *wrong*, so guessing stays bounded at 30/min while the holder of the
correct key can never be locked out by guessers. Per-IP keying depends on `TRUSTED_PROXY_IPS` being set correctly and on the proxy sending `X-Real-IP`; see the
deployment checklist.

Clean URL rewrites (via nginx `endpoints/rewrite_rules.conf`):

```
/v1/fyp/feed/{username}              →  /rpc/get_personalized_feed
/v1/fyp/global                       →  /rpc/get_global_feed
/v1/fyp/profile/{username}           →  /rpc/get_user_profile
/v1/fyp/algorithm                    →  /rpc/get_algorithm
/v1/fyp/algorithms                   →  /rpc/list_algorithm_versions
/v1/fyp/explain/{username}/{post_id} →  /rpc/get_score_explanation
/v1/fyp/feed-metrics                 →  /rpc/get_feed_metrics
/sync-status                         →  /rpc/get_sync_status
/health                              →  /rpc/get_health
/version                             →  /rpc/get_version
<anything else>                      →  /rpc/<anything else>      (catch-all)
```

The rewriter fronts the **PostgREST surface only** — `docker/haf_fyp_nginx.conf.template`
proxies to PostgREST and deliberately does not proxy the FastAPI overlay. So `/health` in
this list is PostgREST's `get_health` (an array), which is a different endpoint from the
FastAPI `/health` in the table above (an object), and different again from the rewriter's
own container-liveness `/health` on port 81 (a bare 204). The catch-all is why any path
matching no rule above — including the retired `/v1/fyp/metrics` — is rewritten to
`/rpc/<path>` and comes back 404 rather than as a routing error.

## Ranking Algorithm

**Version 1.0.0** — four-signal weighted sum with community boost:

```
final_score = 0.40 × relevance
            + 0.30 × recency
            + 0.20 × engagement
            + 0.10 × credibility

if post.community_id ∈ user_subscribed_communities:
    final_score = min(1.0, final_score × 1.20)
```

| Signal | Range | Source |
|--------|-------|--------|
| `relevance` | [0, 1] | Cosine similarity: post embedding vs user interest vector (HiveSense 1024-dim) |
| `recency` | [0, 1] | `exp(−ln2 × age_hours / 24)` — 24-hour half-life |
| `engagement` | [0, 1] | Sigmoid-normalised unique commenters + reblogs + net rshares |
| `credibility` | [0, 1] | 0.6 × normalised reputation + 0.4 × log-normalised VESTS |
| `community_boost` | ×1.20 | Applied post-sum for on-chain subscribed community posts |

All weights are stored in `haf_fyp_app.algorithm_versions` and activated via `POST /v1/fyp/admin/algorithm`. The active version and its weights are returned by `/rpc/get_algorithm`. Every feed response includes per-signal component scores for full transparency; use `/rpc/get_score_explanation` for a detailed breakdown of any individual post.

**Cold start:** for new users without an interest vector, relevance is dropped and the remaining three weights are renormalised to sum to 1.0. Bootstrap priority:

1. Recent votes and reblogs → weighted average of post embeddings
2. Stored vector from a previous ranker cycle
3. Average embedding of top posts in on-chain subscribed communities
4. Average embedding of top posts in declared interests (`POST /v1/fyp/interests`)
5. No vector — cold-start weights applied (no relevance signal)

Without HiveSense, steps 1, 3 and 4 find nothing. Step 2 is different: it reads a
**stored** vector from `haf_fyp_app.user_profile_vectors`, a haf_fyp-local table
HiveSense cannot affect, so a node that once had HiveSense still has one for every
previously-ranked user.

That used to matter. `compute_scores` dropped the relevance weight only when the
user vector was `None`, so those users kept `w_v = 0.40` while relevance was a flat
0.5 for every post — neutral relevance at *full weight*, the thing this section's
title says does not happen. It now drops the weight whenever relevance cannot be
computed at all, which is the case with no post embeddings regardless of what is
stored. See
[Without HiveSense there is no personalization](#without-hivesense-there-is-no-personalization-not-neutral-relevance).

(Before #41, steps 1, 3 and 4 *raised* rather than finding nothing, so affected
users got no cached feed at all; all four call sites now share one guard.)

### How fresh a feed is

Feeds are computed ahead of time and served from `feed_cache`; a request never ranks
anything. So a feed is as old as the last time the ranker reached that account.

The ranker's rotation holds every account there is something to personalize for, which
on a full node is tens of thousands, and a scheduled cycle ranks `MAX_USERS_PER_CYCLE`
of them, then waits `CYCLE_SECONDS` (300). Ranked round-robin, an account was reached
once per full rotation: about 22 hours at 45,000 accounts and the default 200.

**Readers go first (#81).** Requesting the first page of a feed tells the ranker, which

1. records the read (`active_users.last_read_at`);
2. re-ranks that account if its feed is older than `READ_REFRESH_MIN_AGE_SECONDS`, so
   the *next* load is current, as long as there is budget left for the cycle
   (`READ_REFRESH_MAX_PER_CYCLE`);
3. takes accounts that read within `READER_WINDOW_SECONDS`, and whose feed is older
   than `READ_REFRESH_MIN_AGE_SECONDS`, at the front of every scheduled cycle, stalest
   first, in at most `READER_CYCLE_SHARE` of the cycle.

The rest of each cycle goes to everyone else in the old order. The first request after
a long absence is still served the old feed; it is the one that triggers the refresh.

These are environment variables of the `haf-fyp-ranker` service.
`docker/docker-compose.yml` passes each one to the ranker when it is set, so add the
line to your `docker/.env.local` (`MAX_USERS_PER_CYCLE=3000`; a copy made before this
existed has no line for it) and recreate the ranker with the same `--env-file` as every
other command:

```bash
docker compose --env-file docker/.env.local -f docker/docker-compose.yml \
  up -d --build --no-deps haf-fyp-ranker
```

(Plus your stack's `-p`, if it has one.) `--build` matters the first time: a ranker
image built before these settings were passed stops on a line left blank. `--no-deps`
keeps the command to the ranker; it also means Redis is not started if it is down.

Not set, or left blank, a variable takes the default below. A variable of the same
name exported in your shell outranks the file, and lasts only as long as that shell:
the next `up` of the ranker without it puts the ranker back. haf_api_node's
`haf_fyp.yaml` does not pass them yet (#87): there, add them to that service's
`environment:`.

The ranker reads six more settings that are not passed and not listed here (retention
and maintenance: `FEED_EVENTS_*`, `EMBEDDING_COVERAGE_DAYS`, `ACTIVE_USERS_*`). Changing
one of those still means adding it to the service's `environment:`.

| Variable | Default | What it bounds |
|---|---|---|
| `MAX_USERS_PER_CYCLE` | 200 | Accounts ranked per scheduled cycle. At about 0.2 s each the default uses a fraction of the cycle; raising it shortens the rotation for everyone. |
| `READER_CYCLE_SHARE` | 0.5 | Share of a cycle recent readers may take (capped at 0.9). 0 turns the reader lane off. |
| `READER_WINDOW_SECONDS` | 86400 | How long one read keeps an account eligible for the reader lane. |
| `READ_REFRESH_MIN_AGE_SECONDS` | 600 | A feed younger than this is not re-ranked on read, and does not take a lane slot. |
| `READ_REFRESH_MAX_PER_CYCLE` | `MAX_USERS_PER_CYCLE` x `READER_CYCLE_SHARE`, at most 300 | Immediate re-ranks between two scheduled cycles. 0 turns them off. They run in the 300 s the ranker waits between cycles: keep this times seconds-per-account well under `CYCLE_SECONDS`, or they start delaying the cycle. |
| `READ_REFRESH_BATCH_SECONDS` | 30 | After one read-triggered re-rank, the next waits this long and takes everyone who became due meanwhile. Each one fetches the candidates before ranking anyone (about 8 s on a full node), so they are grouped. 0 turns the grouping off. |

What to expect, and what this does not do:

* **It is not immediate.** With the ranker between cycles, the refresh takes about 8 s
  once the ranker looks, which is up to 5 s after the read (one measurement on a full
  node: 9 s end to end), or up to `READ_REFRESH_BATCH_SECONDS` later if another reader
  was served just before. The ranker hears reads between passes of its loop, so a read that arrives
  during a scheduled cycle is handled when that ends: under a minute at the default
  cycle size, several minutes on a node that ranks thousands per cycle. And once the
  cycle's budget is spent, a stale reader waits for the reader lane instead.
* **Everyone else rotates more slowly while the reader lane is in use**, by up to
  `1 / (1 - READER_CYCLE_SHARE)`: twice as slowly at the default when the lane is full.
  That includes the first load of someone who has not read for longer than the window.
* **A read is not proof of a reader.** The feed endpoint is public and takes any
  username, so anyone can make accounts look read. Requesting every account's first
  page puts the whole rotation in the lane, and real readers are back to the old
  round-robin for the length of the window. The cheaper way to hurt is smaller: keeping
  about twice the lane's slots in the window (200 accounts at the defaults: an account
  ranked in one cycle is under the minimum age for the next) holds everyone else at
  `1 - READER_CYCLE_SHARE` of the old rotation speed, half at the default, for as long
  as it is kept up. That is the worst this can do. Telling a reader from a crawler
  needs an authenticated read, which this app does not have.
* **Paging.** A feed rewritten while someone scrolls can repeat or skip a post between
  page 1 and page 2. Readers' feeds are rewritten more often than others'.
* **The signal is visible inside the database.** It is a Postgres notification on the
  channel `haf_fyp_feed_read` carrying the username. Any role with a session on the
  database can listen to it, and can also send on it, which is one more way a read can
  be forged; what the ranker does with one is bounded the same either way.
  `last_read_at` is not exposed by any endpoint.

After every scheduled cycle the ranker logs one line:
`feed reads since the last cycle: N account(s) heard, M queued for an early re-rank; reader lane L of S slot(s)`.
(After a cycle that failed, the lane figures read 0 of 0.)
A lane that stays full means the rest of the rotation is running slower. A heard count
in the thousands means someone is requesting other people's feeds. Nothing heard for
days on a node whose feed is in use means the signal is not arriving; a zero now and
then is normal, because a returning reader is only heard once in ten minutes.

**What a notifying request costs.** It takes a transaction id and a brief cluster-wide
lock at commit, so the feed function rations it: first page only, and at most once per
account per ten minutes *once the ranker has recorded the read*. The ranker records
reads between passes, so during a scheduled cycle, and for as long as the ranker is
stopped or older than this change, every first-page request notifies. If the ranker is
going to be down for longer than a deploy, switch the signal off:

```sql
-- as a superuser (haf_admin)
ALTER ROLE haf_fyp_user SET haf_fyp.feed_read_notify = 'off';
-- and to switch it back on
ALTER ROLE haf_fyp_user RESET haf_fyp.feed_read_notify;
```

PostgreSQL applies a role's settings when a session starts, so this reaches the feed
function as the database sessions PostgREST uses are replaced. The way that restarts
nothing is to end that role's idle sessions, from a superuser session connected to
PostgreSQL itself, and let the pool open new ones:

```sql
SELECT pg_terminate_backend(pid) FROM pg_stat_activity
 WHERE usename = 'haf_fyp_user' AND application_name LIKE 'PostgREST%' AND state = 'idle';
```

Run it twice, a few seconds apart. This was exercised on a node with PostgREST connected
directly: no request failed, and the next first-page read did not notify. Behind
PgBouncer (`haf_api_node`) the sessions that matter are PgBouncer's server connections,
which do not carry PostgREST's `application_name`. Left alone, they are replaced within
PgBouncer's `server_lifetime` (an hour by default), so the switch applies itself. To
apply it sooner, drop the `application_name` condition; that also ends the idle
sessions of `haf-fyp-api`, which logs in as the same role and reconnects, and a
request in flight on a connection ended this way can fail once. That layout has not
been exercised. Restarting `haf-fyp-postgrest` also works when it connects directly,
and changes nothing behind PgBouncer.

To check it took, request a feed and watch `LISTEN haf_fyp_feed_read;` from a psql
session that keeps issuing a statement (a notification is shown after the next one),
and is not left inside a transaction.

With `READER_CYCLE_SHARE=0` and `READ_REFRESH_MAX_PER_CYCLE=0` the ranker ranks the
same accounts in the same order as before #81, at once. With only the signal off it
gets there once the reads already recorded are older than `READER_WINDOW_SECONDS`.
Either way it still listens, and still writes its line each cycle.

`pg_notification_queue_usage()` is the thing to watch if a ranker is ever left paused
(not stopped) for days, or a psql session is left listening inside an open transaction:
a listener that stops reading pins the cluster's notification queue.

**Upgrading.** The ranker needs the `last_read_at` column, which the install adds in
place. A ranker started against SQL that predates it ranks without the reader lane and
says so in a warning, repeated hourly. On a HAF with application locks it stays that
way until someone stops it: the ranker itself holds the lock the install waits for, so
an install run while it is up installs nothing (see
[Re-installing while the stack runs](#re-installing-while-the-stack-runs-hafs-application-locks)).
Stop the block processor and the ranker, run the install, start them. On a HAF without
those locks the ranker notices the column after its next cycle.
The other way round, new SQL under an old or stopped ranker, nothing breaks,
but nothing records the reads either, so every first-page request notifies: switch the
signal off for the duration.

## Database Schema

```
haf_fyp_app
├── post_candidates       — root posts eligible for ranking
├── post_lookup           — (author, permlink) index for processor joins
├── post_commenters       — unique commenter pairs per post
├── engagement_metrics    — comment/reblog/vote counts, net rshares
├── user_profile_vectors  — 1024-dim interest vector per user (pgvector)
├── feed_cache            — precomputed top-200 ranked feeds
├── global_feed_cache     — unauthenticated global feed
├── user_communities      — on-chain community subscriptions
├── user_interests        — cold-start picker declarations
└── algorithm_versions    — weight registry with is_active flag

haf_fyp_endpoints
└── feed_post_row         — composite type returned by feed functions
```

## Services

```yaml
haf-fyp-setup      # one-shot DDL install + cross-schema grants (Dockerfile.sql)
haf-fyp-sync       # PL/pgSQL block loop (Dockerfile.sql)
haf-fyp-uninstall  # removes the app from the database — profile "haf-fyp-uninstall",
                   # never starts on a plain `up` (Dockerfile.sql)
haf-fyp-ranker     # Python ranking daemon, 5-minute cycles (Dockerfile.python)
haf-fyp-api        # FastAPI overlay on port 3002 (Dockerfile.python)
haf-fyp-postgrest  # PostgREST on port 3000 (raw /rpc/, for debugging)
haf-fyp-postgrest-rewriter
                   # public read API on port 3001 (clean /v1/fyp/... paths)
haf-fyp-redis      # pub/sub for force-refresh signals + rate-limit buckets
haf                # the HAF node itself. The ONE service without the app prefix, and
                   # deliberately so: it matches haf_api_node's own service name, which
                   # is what makes `POSTGRES_HOST: haf` correct both here and in a node
                   # module. See the header of docker/docker-compose.yml (#48).
```

Every other service carries the `haf-fyp-` prefix. That is not cosmetic: compose
identifies a service by project and name, so a same-named service from another file is not
a second service. Run under a node's project name, this file's `haf` **recreates** the
node's HAF container from this file's definition. Layered with `-f` after the node's
files, it overrides the node's image, healthcheck and datadir mount (most Compose versions
do this silently).
`include:`d, Compose v5 merges it silently too. A `haf_api_node` never does any of this
with this file — it runs apps from module yamls, and haf_fyp's lives outside this repo —
so the exposure is latent, and the prefix keeps it latent by decision rather than by luck.
**Never combine this file with a node's compose files, and never run it under a node's
project name.** The header of `docker/docker-compose.yml` has the measured versions, and
`tests/unit/test_compose_contract.py` pins the naming rule.

The same applies one level up, on a single host. The stack's own project name is pinned
to `haf_fyp` (#53). Unpinned, it was the compose file's directory, `docker`, which every
stack started from a `docker/` directory shares. Its volume names follow the project
(`haf_fyp_haf_datadir`, `haf_fyp_redis_data`; `<p>_…` under `-p <p>`), and
`HAF_FYP_HAF_DATADIR_VOLUME` in `.env.local` lets a stack that ran under the old default
project, `docker`, keep its HAF data.
Two checkouts on one host now share `haf_fyp`, so run a second one under its own `-p`.
`docker-compose-ci.yml`,
which `scripts/run_tests.sh` runs on the host, is pinned to `haf_fyp_ci` for the same
reason.

Run that suite locally with `./scripts/run_tests.sh`. It exits with Tavern's code, prints
the logs of the services that matter (`setup` first — a SQL error there fails the `up`
itself, since every service waits on it), and tears the stack down on the way out whatever
happened, including on Ctrl-C. To keep the stack and inspect it instead — `psql` into it,
curl the rewriter — use `./scripts/run_tests.sh --keep`; the script says so too when a run
fails. A kept stack survives only until the next run, which starts by removing it
([#55](https://gitlab.syncad.com/hive/haf_fyp/-/issues/55)).

### The SQL image's verbs

`Dockerfile.sql` builds one image used by three services, dispatched by
`docker/docker_entrypoint.sh`:

| Verb | Runs | Notes |
|------|------|-------|
| `install_app [--swagger-url=BASE] [--host=] [--port=] [--user=] [--is_forking=FALSE]` | `scripts/install_app.sh` | idempotent; safe to re-run — but *skipped* while the block processor runs (below) |
| `process_blocks` | `scripts/process_blocks.sh` | infinite loop |
| `uninstall_app [--force] [--host=] [--port=] [--user=]` | `scripts/uninstall_app.sh` | destructive |

This is the trio every haf_api_node app image exposes, which is what lets a module file
say `command: ["install_app"]` rather than reaching into the image for a script path.

Connection settings default from the environment (`POSTGRES_HOST`, `POSTGRES_PORT`,
`POSTGRES_DB`, `POSTGRES_USER`); `--host=` / `--port=` / `--user=` override them, and
the entrypoint forwards them exactly as balance_tracker's does, so a runbook copied
from an upstream app works here. An argument neither option loop recognises is now an
error rather than being silently discarded — which also turns a mistyped
`--swagger-url` from a silent fallback into a failed install.

`--swagger-url=BASE` sets the base URL the published OpenAPI document advertises in
`servers` (`GET /haf-fyp-api/`). It takes an absolute path (`/haf-fyp-api`, or `/` on a
standalone stack) or an http(s) URL with a path. A bare hostname is not a base URL: it
is ignored with a warning in the install log, and the default `/haf-fyp-api` stands.
The value is stored in `haf_fyp_app.app_config`, so the next install without the flag
(or with an empty value) puts the default back. The standalone stack in
`docker/docker-compose.yml` passes `/`, because there the API is served at the root of
its port; set `FYP_SWAGGER_URL` in `docker/.env.local` to pass something else. Like the
rest of an install, the setting is not applied while the block processor runs: the
install is skipped (see "Re-installing while the stack runs"), and with it the warning
about a value that will be ignored.

To change it on a running node without an install, run the one file the installer
runs. It takes effect on the next request; nothing needs restarting:

```bash
psql -X -v ON_ERROR_STOP=on -v swagger_url=/haf-fyp-api -f scripts/set_swagger_base_url.sql
```

The next install sets it again from its own flag, so put the value there as well.

`--is_forking` is accepted, and **only `FALSE` is honoured**. haf_fyp registers a
**non-forking** HAF context by design (`hive.app_context_set_non_forking`,
`db/haf_fyp_app.sql`), so HAF does not rewind its tables on a fork. `TRUE` — which a
module file copied from an upstream app carries by default, from a node-wide
`${IS_FORKING:-TRUE}` nobody chose for this app — logs a warning and continues. It
deliberately does not fail: `haf-fyp-setup` runs under `restart: on-failure` while
every other service waits on `service_completed_successfully`, so failing there would
crash-loop the installer over a flag that is a no-op here.

Anything that is not a verb but *is* runnable is still executed, so
`run --rm haf-fyp-setup /app/scripts/grant_cross_schema.sh` and
`docker run --rm <image> bash` keep working. An argument starting with `-` goes to bash,
preserving `docker run --rm <image> -c '...'` — except `--help`, `-h` and `-?`, which
print the dispatcher's own usage.

Moving these services from a script path to a verb needs `--build` on the first run
after upgrading: they declare `build:` with no `image:`, so a plain `up -d` reuses a
previously-built image, which has no `install_app` to run.

### Re-installing while the stack runs: HAF's application locks

On a HAF that has them (1.28.8-rc1 and later), the install and the block processor no
longer run over each other ([#67](https://gitlab.syncad.com/hive/haf_fyp/-/issues/67)). The
block processor, through the driver or the legacy `CALL main()` loop, holds a shared lock
on `haf_fyp`, and on `hivemind`, whose tables it reads, for as long as it runs. The
ranker, which writes `haf_fyp_app`'s tables in long transactions, holds the same shared lock
on `haf_fyp` on its database session
([#77](https://gitlab.syncad.com/hive/haf_fyp/-/issues/77)) — only that one: it reads
hivemind, hivesense, balance_tracker and reputation_tracker only as optional signals, so a
cycle that meets one of their installs degrades, waits, or is skipped, and the next one
heals. The API holds none yet (#77).
`install_app` takes the `haf_fyp` lock exclusively through the psql image's
`install_with_app_lock.py`, the same way balance_tracker, haf_block_explorer,
reputation_tracker, hivesense and nft_tracker do.

So **re-running `install_app` while the block processor or the ranker is up installs
nothing.** The log says `Skipping haf_fyp install — lock held by an active block-processor`,
names the holder (`haf_fyp_ranker` for the ranker), and the install service still exits 0 —
so nothing else looks wrong, and the processor, ranker and API carry on against the old
SQL. `GET /version` still reporting the previous commit is the tell.

**A whole-stack `up -d --build` upgrades in the right order**: both services are
recreated, and Compose stops every replaced container before starting any, so the old
processor and ranker are gone before the install runs (`depends_on … restart: true` on both
covers the case where only `haf-fyp-setup` changed). **Running the install on its own** —
`up haf-fyp-setup`, `run haf-fyp-setup`, `restart haf-fyp-setup` — leaves both running and
holding the lock, so stop them first, and rebuild them too when you start them again (each
has its own image, and a plain `up` reuses it):

```bash
export GIT_HASH="$(git rev-parse --verify HEAD | cut -c1-8)"   # as in Quick Start step 2
docker compose --env-file docker/.env.local -f docker/docker-compose.yml stop haf-fyp-sync haf-fyp-ranker
docker compose --env-file docker/.env.local -f docker/docker-compose.yml up --build haf-fyp-setup
docker compose --env-file docker/.env.local -f docker/docker-compose.yml up -d --build haf-fyp-sync haf-fyp-ranker
```

**haf_api_node**: an upgrade to a new `HAF_FYP_VERSION` recreates both services and is safe
the same way. Running `haf-fyp-install` on its own, or a module change that recreates only
it, is not — its module has no `restart: true` (hive/haf_api_node#47) — so stop the
processor and the ranker first there:

```bash
docker compose stop haf-fyp-block-processing haf-fyp-ranker
docker compose --profile core --profile haf-fyp up -d    # or your usual profile flags
```

(`up -d` without the profile that enables haf_fyp leaves the stopped services stopped and
never runs the install.)

In either deployment a processor killed mid-statement keeps the lock until PostgreSQL
finishes that statement; this repo gives `haf-fyp-sync` two minutes to stop cleanly.

The processor's lock on `hivemind` means the same for hivemind: its installer skips while
haf_fyp is processing, as it already does for hivesense. Stop haf_fyp's processor before
re-installing hivemind.

The processor takes that lock only where hivemind's tables exist
(`hivemind_app.hive_posts`), so a **first** install of hivemind is not blocked: on a new
haf_api_node node haf_fyp's processor can start before `hivemind-install` has run, and it
used to hold the lock there and keep hivemind from ever installing (#79). Two things still
need doing by hand on such a node, until haf_api_node's module orders the two installs
(hive/haf_api_node#47; hivesense's module does it with a `depends_on` on
`hivemind-install`):

- the lock names are decided when the processor starts, so it holds `hivemind` only from
  its next start;
- haf_fyp declares hivemind a dependency only if hivemind is registered when haf_fyp is
  installed, so the install has to run again.

One sequence does both: stop the processor, re-run the install, start the processor.

**Stopping the processor releases the lock.** The legacy `CALL main()` loop cancels its own
statement on SIGTERM, so its session — and the lock — end with the container; if it is
killed outright, the server notices the vanished client within
`client_connection_check_interval` (5s). Before that, a stopped container's session could
outlive it until the server next wrote to it. The ranker does the same: on SIGTERM it
cancels its own statement and exits at once (while the database is reachable — the cancel
is a connection of its own; otherwise Docker kills it at ten seconds), and its session
sets the same interval.

### Block-processing healthcheck

The image ships `/app/block-processing-healthcheck.sh` — the path
`reputation_tracker.yaml` and its siblings hardcode — and `docker/docker-compose.yml`
attaches it to `haf-fyp-sync`. It is **not** declared as a `HEALTHCHECK` in the
Dockerfile: upstream does not, and an image-level check would also land on the one-shot
install and uninstall containers, for which "is the block loop advancing" is
meaningless.

It replaces `psql -c 'SELECT 1'`, which proved PostgreSQL answers and nothing about
whether `haf_fyp_app.main()` — an infinite loop — was still running.

The signal is `hafd.contexts.last_active_at`, HAF's own column, written once per
`hive.app_next_iteration` and committed immediately. Healthy when it is fresher than
`HAF_FYP_HEALTHCHECK_MAX_AGE_SECONDS` (default 300) **and** later than this container's
start, or when `hive.is_app_in_sync()`.

Be precise about what that column is: it is **not** a sub-batch heartbeat, and it is
written with a clock that is already one iteration old. `__now TIMESTAMP := NOW()` is a
DECLARE initializer evaluated at procedure entry, and `NOW()` is
`transaction_timestamp()` — the start of the caller's still-open transaction, i.e. the
one that just did the *previous* batch. So the value committed at the end of iteration
*k* carries iteration *k−1*'s start, and the age this check observes spans up to **two**
iteration durations.

What it adds over the block pointer is the other case: at the chain tip the loop keeps
iterating while waiting for a block, so the heartbeat ticks even though nothing was
processed, which separates "idle" from "wedged".

The consequence, stated rather than hidden: on a long MASSIVE batch this check **can
report unhealthy on a healthy replay**. The 300s default (upstream uses 60s) is a wide
guess, not a measurement — and because of the one-iteration lag it tolerates iterations
of roughly 150s, not 300s. It is explicitly *not* justified by this app's 360s HAProxy
agent-check, which measures how old the last processed *block* is, a different quantity.

`SET TIME ZONE 'UTC'` is deliberately absent, diverging from upstream.
`last_active_at` is `timestamp without time zone` written in the *server's* zone, so
forcing the reader to UTC does not convert it — it only assumes it, and on a server
ahead of UTC the difference goes negative and a dead loop reports healthy forever.
Comparing `now()::timestamp` keeps both sides in the server's own zone, and
`scripts/process_blocks.sh` takes the startup timestamp from `SELECT now()::timestamp`
for the same reason — the cast is the point, not an incidental.

**The HAProxy agent-check is a separate mechanism and still lives outside the image**;
see `deployment/healthchecks/UPSTREAM_INTEGRATION.md`.

## Uninstalling

**Destructive and irreversible.** Drops the HAF context, both schemas `CASCADE`, and
both roles — including the ranker's entire accumulated history, which only a full
reprocess rebuilds. There is no backup step.

Everything that connects as a haf_fyp role must be stopped first — not just the block
processor. `DROP SCHEMA ... CASCADE` needs an `AccessExclusiveLock`, so with the ranker
or the API still holding sessions it would **block** rather than fail, after the HAF
context had already been removed. The script refuses while any such session exists.

### If another application owns the context

The script also refuses, before changing anything, when a **different application** in the
HAF registry holds haf_fyp's context — or any context whose tables live in a schema this
teardown drops ([#66](https://gitlab.syncad.com/hive/haf_fyp/-/issues/66)). That happens on
a node where haf_fyp is *embedded* in another application: since haf#345 the host
application's registration carries the embedded app's contexts, and haf_fyp's own install
warns and leaves itself unregistered
([#62](https://gitlab.syncad.com/hive/haf_fyp/-/issues/62)). Uninstalling anyway would strip
the context from that application, unregister it outright if it was its only one, and drop
the context's state table — `hive.app_remove_context` ends in `DROP TABLE <schema>.<context>`
— while its driver is still processing blocks.

**At install time** the same state stops `install_app.sh` at step 0 when the context is owned
by a different *role* as well: the install refuses, names the application, and changes
nothing ([#75](https://gitlab.syncad.com/hive/haf_fyp/-/issues/75)). (When the role is
haf_fyp's own, the install goes ahead and leaves itself unregistered, as above.) There is
nothing to repair on haf_fyp's side: either leave haf_fyp off that node — stop
`haf-fyp-setup`, or `haf-fyp-install` in haf_api_node — and let the other application
process the context, or install haf_fyp on a HAF where it owns its context. Do **not**
`hive.app_remove_context('haf_fyp')` to make the install pass; that is the teardown this
section exists to prevent.

To remove haf_fyp there, detach the context first. **Not** by re-registering the other
application with fewer contexts: `hive.app_register` also sets `process_procedure` and
`completed_block_function` from its arguments, so that blanks its driver wiring, and with an
empty group it does nothing at all (`CARDINALITY(contexts) > 0` is a CHECK, and
`contexts @> '{}'` matches every other row). Edit the array instead, as `haf_admin` or a role
that can impersonate its owner:

```sql
UPDATE hafd.applications
   SET contexts = array_remove(contexts, 'haf_fyp')
 WHERE name = '<the other application>';
```

If that context is the application's **only** one there is no detach — the same CHECK
forbids it — so removing haf_fyp means removing that application as well:

```sql
SELECT hive.app_unregister('<the other application>');
```

It keeps its position (`hafd.contexts` is untouched), but every dependency row it takes part
in is deleted in **both** directions, and re-registering later does not restore them.

If the other application is yours and this is deliberate — you are tearing down the host
application along with the haf_fyp it embeds — pass `--allow-foreign-context` and the
teardown goes ahead, still printing every owner and what each one loses. It is **not**
implied by `--force`: `--force` only means "there is no TTY, do not prompt", and the
`uninstall_app` verb in `docker/docker_entrypoint.sh` always passes it, so overloading it
would disable this guard on precisely the automated runs nobody is watching. There is no
environment variable for it either — an override sitting in an `.env` file is a guard that
has quietly stopped existing.

Worth knowing what the rest of the ecosystem does here, because it is not this. In
balance_tracker, haf_block_explorer and reputation_tracker the teardown calls
`hive.app_remove_context` behind nothing but `hive.app_context_exists` — all three were
written before the registry existed (`src/hive_fork_manager/app_registry.sql` landed in HAF
on 2026-08-25). hivesense has adapted, but only for itself: it `app_unregister`s its own
row *before* removing its contexts, so the registry hook finds nothing to rewrite. None of
the four asks who owns the context. The *install* side is further along — balance_tracker
and reputation_tracker both skip registration with
`Context % already belongs to application %` when another application holds it, which is
the same check haf_fyp makes in `db/register_app.sql`.

Block processing refuses the same state for the same reason: `scripts/process_blocks.sh`
will not drive a context another application owns, because two loops advancing one context
pointer lose blocks for both. It also will not start while it *cannot find out* — a failed
ownership query is not read as "nobody" ([#74](https://gitlab.syncad.com/hive/haf_fyp/-/issues/74)).
It logs the error and retries, doubling the wait from 10 seconds up to five minutes. (The
first wait is `HAF_FYP_OWNERSHIP_RETRY_SECONDS` if that is set in the block-processing
service's `environment:` -- neither compose file passes it through, so `.env` alone does not
set it.) If the error is a missing `haf_fyp_app.context_claimed_by_other_app()`, the database
predates this image: re-run the install as in step 2 above (`up --build haf-fyp-setup`; in
haf_api_node, the `haf-fyp-install` service). In both states the container's log says why,
and so does its healthcheck ([#76](https://gitlab.syncad.com/hive/haf_fyp/-/issues/76)): it
fails with the reason ("parked: …" or "waiting: …"), whatever the context's heartbeat says —
another application driving the context keeps that fresh. Docker does not count failures
during the 72-hour `start_period` until a check first succeeds, and a parked container's
never does, so for those 72 hours it shows `health: starting` rather than `unhealthy`; the
reason is in `docker inspect --format '{{json .State.Health}}'`. Docker's health is the
only signal for a park: `/sync-status` and haf_api_node's HAProxy agent-check read the
context's block pointer, which the owning application keeps advancing, so they stay
green, and haf_api_node's `stack_status.sh` lists a `health: starting` container as `up`.

```bash
docker compose --env-file docker/.env.local -f docker/docker-compose.yml \
  stop haf-fyp-sync haf-fyp-ranker haf-fyp-api haf-fyp-postgrest
docker compose --env-file docker/.env.local -f docker/docker-compose.yml \
  run --rm haf-fyp-uninstall
```

Keep `--env-file` on the `run` as well as the `stop`. `run` recreates any dependency whose
configuration differs. So if `.env.local` changes `HAF_REGISTRY` or `HAF_VERSION` — and
the quick start tells production to pin `HAF_VERSION` — leaving it off recreates `haf`
from `docker/.env`'s values just before the teardown. Since #56 that matters more, not
less: this repo's pin now resolves, so the recreate succeeds on a different HAF rather
than failing on the pull, and against a synced datadir a HAF from another line refuses to
start (see the replay warning under Prerequisites). Add the stack's `-p` to both
commands if it runs under one. A stack started before #53 must first be moved with
`scripts/upgrade_standalone_stack.sh` (see *Upgrading a standalone stack from before #53*
above). Unless it was started with `-p` or `COMPOSE_PROJECT_NAME`, its containers belong
to the old default project, `docker`, which `stop` here does not reach, and they would
keep their sessions open.

The profile keeps it out of a plain `up`, and is namespaced `haf-fyp-uninstall` so
`COMPOSE_PROFILES` cannot collide with another app's teardown on a shared node.

For a confirmation prompt, run `scripts/uninstall_app.sh` directly without `--force`:
it asks you to type the database name, and refuses outright if there is no TTY to ask
on.

Either way it first asks the server which server it is, and prints that next to what was
requested: address, port, database, role and cluster id (`reached  : ...`). The two can
differ without any flag being wrong, because `PGHOSTADDR` or a `pg_service.conf` entry
redirects the connection while `--host` still says the original name; `PGHOSTADDR` and
`PGSERVICE` are called out when set. If the server cannot answer, nothing is removed. The
name you type at the prompt is compared with the database the server reports, and the
inventory of what is about to be removed is printed on the `--force` path too.

Two limits. A redirected `--force` run is warned about, not refused. And each `psql` call
opens its own connection with the same settings, so with a multi-host `POSTGRES_HOST`, DNS
round-robin or a pooler in front of several servers, a later call can reach a
different server than the one that answered; the cluster id is also shared by physical
copies of one data directory (replicas, snapshot clones).

It reports what it could not remove rather than stopping at the first error, so a
partial teardown is visible instead of silent; re-running is safe.

Not removed, because this app did not create them and they are shared: the `pgvector`
extension, and grants held by other roles on `hivesense_app` / `reptracker_app` /
`btracker_app`.

## Deployment Checklist

Before going live:

- [ ] `pgvector` extension installed by superuser on the HAF PostgreSQL server
- [ ] `FYP_ADMIN_API_KEY` set to a strong random value in `docker/.env.local`
- [ ] **`TRUSTED_PROXY_IPS` set to the reverse proxy's address or CIDR** (or the literal `direct` if nothing fronts this app). Since #28 this also gates *when* limits are charged: with it unset the app logs an ERROR at startup and falls back to charging only after validation, because charging a request before it is validated is safe only when the bucket identifies the caller — into one shared bucket it lets a single sender 429 everybody else, which is worse than the bug #28 fixes. Rate limits key on `X-Real-IP` only from a trusted peer; unset, every caller keys on the proxy's address instead — one shared bucket for all of them, which is no per-client limit at all. The app logs a warning at startup when it is unset, and once more if a peer outside the list presents the header (which usually means the proxy was recreated with a new address and the list is now stale). Re-check it after anything that can renumber the proxy container.
- [ ] **The reverse proxy sends `X-Real-IP`.** Setting `TRUSTED_PROXY_IPS` only says whose header to believe; the outermost proxy still has to set one (set, not append) to the client's address, and every proxy after it has to pass it on. In Caddy that is `header_up X-Real-IP {client_ip}` on the haf_fyp routes, which `haf_api_node`'s `caddy/Caddyfile.tmpl` carries wherever it has the haf_fyp route (the 1.28.8 series) — a local Caddy snippet with its own `reverse_proxy` for a haf_fyp path needs the line too. Requests from a trusted peer without a usable header (absent, empty, not an address, or naming the peer itself) all share that peer's bucket, as with an empty list: they are charged only after validation, and the app logs one warning per process naming the peer the request came from (`Trusted peer ... sent a request with no usable X-Real-IP`). To confirm: restart the API, send one `POST` to the events endpoint through the edge (`/haf-fyp-admin/v1/fyp/events` on `haf_api_node`; only the four rate-limited routes can raise it, and any body will do), and check that the log since that start has neither this warning, nor the `not in TRUSTED_PROXY_IPS` one, nor the startup error about an unset list. This proves the header only if the trust list matches the peer: with a list that is set but wrong, a missing header is silent.
- [ ] `HAF_VERSION` pinned to a specific release tag — not `latest`, which moves, and not a commit tag, which the registry sweeps once it falls out of the five newest — at `1.29.0-rc2` or newer so the app registers itself instead of falling back to the legacy loop. Pinned **before the first sync**: changing it later is a replay, not an upgrade (see Prerequisites)
- [ ] **If Hivemind was installed AFTER haf_fyp started syncing: `CALL haf_fyp_app.run_hive_post_id_backfill();`** — the per-block resolver only covers a rolling 7-day window, so without this every older post stays unresolved and the feeds stay empty. `/metrics` cannot detect this yet ([#43](https://gitlab.syncad.com/hive/haf_fyp/-/issues/43)) — run the backfill on the strength of the install order alone.
- [ ] **Hivemind installed and synced** → `grant_cross_schema.sh` re-run. REQUIRED: with hivemind ABSENT every feed returns `[]`. (A lost *grant* on an already-running node is different — resolution stops, but already-rendered posts keep serving from `post_bridge_cache`, so the outage is delayed rather than immediate.) Confirm with `GET /metrics` → `ranking_sources.degraded` is `false`, not by assuming — alert on that rather than on `sources.hivemind == "readable"`, which misses both the `readable_unverified` case and everything the servability probe sees. (Losing `SELECT` on the table does at least `RAISE WARNING` from the resolver; an absent schema — or a revoked `USAGE` on it, which makes `to_regclass` return NULL — is entirely silent.)
- [ ] HiveSense installed and synced → `grant_cross_schema.sh` re-run (optional; without it nobody is personalized — see [Without HiveSense there is no personalization](#without-hivesense-there-is-no-personalization-not-neutral-relevance))
- [ ] balance_tracker installed and synced → `grant_cross_schema.sh` re-run (optional)
- [ ] reputation_tracker installed and synced → `grant_cross_schema.sh` re-run (optional)
- [ ] Verify `hivesense_app.posts_vectors` on live instance: `\d hivesense_app.posts_vectors` (expected: `embedding halfvec(1024)`, keyed by hivemind post id, one row per chunk)
- [ ] Smoke test all endpoints after first ranker cycle completes

## Operating the rate limits

Two levers, both read from the environment by slowapi at startup, neither needing a
code change:

| Variable | Effect |
|---|---|
| `RATELIMIT_ENABLED=false` | Turns *all* rate limiting off — the per-IP limits, the shared ceilings, and the pre-validation charging. Note this also restores the #28 amplifier: malformed batches become free again. |
| `RATELIMIT_SWALLOW_ERRORS` | **Leave unset.** This app already fails **open** on a limiter-storage error on every path (see below) and logs it once a minute. Set, slowapi swallows the error itself, before the app's handler sees it, and logs a full traceback for every request. slowapi treats **any non-empty value** as on, `0` and `false` included. |
| `RATELIMIT_IN_MEMORY_FALLBACK_ENABLED=1` | Same rule about values. On a storage error slowapi then counts in process memory, so requests are still limited, per worker, instead of served unmetered. |

**Failure policy: the limiter fails open.** If Redis is unreachable or slow, a request is
served *unmetered* rather than rejected — limits protect against abuse, they are not part
of answering correctly, so a Redis incident degrades limiting instead of the service. This
holds whichever path does the metering: the pre-validation middleware for most requests
while `TRUSTED_PROXY_IPS` is set, slowapi's own decorator while it is empty and for requests
from a trusted peer with no usable `X-Real-IP` (#78; before that, the decorator's path
answered 500). Storage failures are logged once per minute with a
suppressed-occurrence count, and again immediately if a *different* kind of failure
appears. `/health` reports the limiter's storage in `rate_limit_store` and stays 200.

**Redis coupling.** Limiter buckets live on the same Redis as the ranker's
`haf_fyp:refresh` pub/sub. There is no key collision (`LIMITER/…` vs a channel) and no
unbounded key growth (every bucket carries a window TTL), but note that `limits`
increments on *every* checked request including ones it then rejects, so a flood on the
unauthenticated `/v1/fyp/events` is Redis write traffic. `MAX_REQUEST_BODY_BYTES`
(default 256 KiB) and the per-IP limits are what bound it.

**The Redis service is named `haf-fyp-redis`, and it must not be an evicting
instance.** Both matter wherever haf_fyp runs on a `haf_api_node` that runs jussi, whose
`jussi.yaml` (included when `JSONRPC_API_SERVER_NAME=jussi`) defines a service called
`redis`. A same-named service is not a second service: under the node's shared project
name, whichever file was applied last recreates the one container, so a generically-named
Redis in a haf_fyp module file would silently become jussi's instance, or replace it.
Jussi's runs
`--maxmemory 8G --maxmemory-policy allkeys-lru`, and under `allkeys-lru` the limiter's
buckets are evicted under memory pressure rather than expiring on their TTL — rate
limits stop holding, with no error raised anywhere. The shipped compose states
`--maxmemory-policy noeviction` on the service for that reason.

Be clear about what that flag does and does not do: it is redis' own default, and a
policy without a `maxmemory` ceiling never fires, so it **declares** the requirement
next to the service rather than enforcing it. It also loses a merge -- if another
file in the same project declares a service of the same name, that file's `command`
can win and this one is dropped silently. The rename is what protects the limiter;
the flag documents why, and starts mattering the day someone sets a ceiling.
**Pointing this at your own instance takes THREE entries, not one**, because the two
clients are separate and read different variables:

| variable | used by | what it drives |
|---|---|---|
| `REDIS_URL` | the limiter's storage | rate limiting only |
| `REDIS_HOST` | `_publish_refresh`, `_redis_subscriber` | pub/sub only |
| `REDIS_PORT` | the same two call sites | pub/sub port only |

All three are interpolated in the compose file, so they are `.env` entries — but set
only `REDIS_URL` and rate limiting moves while **pub/sub stays on `haf-fyp-redis:6379`**,
which is silent by construction: `_publish_refresh` wraps its call in
`except Exception: pass`. Set all three, and keep the `noeviction` policy on whatever
you point them at.

**Deploying a change to this file's limits:** `requirements.txt` is part of the image,
so rebuild it, not a bare `up -d`, which silently reuses the cached image:
`docker compose --env-file docker/.env.local -f docker/docker-compose.yml up -d --build haf-fyp-api`
(plus `-p` if the stack runs under one). `haf-fyp-ranker` builds from the same `Dockerfile.python`, so a plain
`docker compose build` restarts it mid-cycle — rebuild the api service by name if that
matters. Nothing needs ordering and a rollback strands nothing: bucket identities are
unchanged and every key expires with its window.

⚠️ **A stack from before the rename of `redis` to `haf-fyp-redis` (#39) is the exception
to every sentence above.** It is also from before #53, so move it with
`scripts/upgrade_standalone_stack.sh`: that removes the old `redis` container with the rest
of the old stack, and starts `haf-fyp-api` and `haf-fyp-ranker` together on the new
stack's network. Do not upgrade it per service. Under the old project, the orphaned `redis` kept its network alias, so the
ranker could resolve the old instance while the api published to the new one. Force-refresh
then failed with no error on either side, until the ranker's next 5-minute cycle picked
the work up regardless.

## Development Phases

| Phase | Status | What |
|-------|--------|------|
| **1 — Skeleton** | ✅ | Schema, tables, stubs, Docker stack |
| **2 — Post ingestion** | ✅ | `process_posts.sql` — ingest root posts, post_lookup, engagement init |
| **3 — Engagement tracking** | ✅ | `process_engagement.sql` — comments, reblogs, votes, payout windows |
| **4 — Community sync** | ✅ | `process_communities.sql` — subscribe/unsubscribe from custom_json |
| **5 — Python ranker** | ✅ | `scoring.py` — recency + engagement scoring, feed_cache writes |
| **6 — HiveSense** | ✅ | Semantic relevance via pgvector embeddings (1024-dim halfvec, `posts_vectors`, bridged through hivemind post id) |
| **7 — Credibility** | ✅ | btracker VESTS + reptracker reputation, cross-schema reads |
| **8 — Cold start** | ✅ | Interest picker, `POST /v1/fyp/interests`, vector bootstrap chain |
| **9 — Production** | ✅ | Rate limiting, score explainer, algorithm governance |

## Verification Tasks

| Task | Status | Notes |
|------|--------|-------|
| T-0.3 — HAF sync loop API | ✅ Resolved | Superseded: `hive.app_next_iteration` DOES exist and is what this app calls (`db/haf_fyp_app.sql`). The original note read "correct function is `hive.app_next_block()`; `hive.app_next_iteration` does not exist" — kept only as history |
| T-0.7 — community custom_json format | ✅ Resolved | Double-encoded JSON array `["subscribe", {"community": "hive-NNNNNN"}]`; account from `required_posting_auths[0]` |
| T-0.1 — HiveSense table and column names | ✅ Resolved | Live: `hivesense_app.posts_vectors.embedding halfvec(1024)`, keyed by **hivemind post id** (`hive_posts.id`), one row per chunk. haf_fyp's own post_id keyspace does not overlap, so the ranker bridges via (author, permlink) → hivemind → hivesense and averages chunks |

## Checking that the pinned images still exist

`scripts/check_pinned_images.sh` resolves every image this repository pins — everything the
compose files **pull** (`docker/docker-compose.yml`, `docker/docker-compose-ci.yml` and
`docker/docker-compose-ci-install.yml`, through `docker/.env`), plus every `FROM` in `Dockerfile.*` — and fails when one of them no
longer exists in its registry. Twelve references today.

It passes **only when every reference was positively resolved**
([#71](https://gitlab.syncad.com/hive/haf_fyp/-/issues/71)), and says how many that was:
`positively resolved 12 of 12`. Each is one manifest `HEAD` to the registry's HTTP API:

- `200` with a `Docker-Content-Digest` — resolved; the digest is printed.
- `404` from the registry itself (it carries `Docker-Distribution-Api-Version`) — `MISSING`:
  the tag (or digest) is gone. A `404` without that header — a proxy, a misrouted ingress — is
  not taken as a verdict.
- refused on every try — `UNREADABLE`: the repository is gone, renamed or private, and an
  operator's anonymous pull fails the same way. Docker Hub issues a token and then answers
  the manifest `401`; `gitlab.syncad.com` refuses at the token service itself (`403 DENIED`).
  A refusal that does not hold on every try is not taken as a verdict.
- anything else (`429`, `5xx`, no response, a `200` with no digest, a token service that
  fails) is asked again, and after three tries is `UNANSWERED` — which also fails the job,
  with its own message and any rate-limit headers the registry sent, because an image nobody
  could verify was not checked.

Why `HEAD`: Docker Hub rate-limits anonymous `GET`s per source IP, which a shared runner
reaches, and it rejects at the limiter **before** it looks the tag up — so a limited `GET` and
a dead tag both answer `429`. The first attempt at this check (!77) used a `GET`, forgave
`429` as inconclusive, and with it forgave dead pins: one run verified 3 of 10 images and
reported success. Hub does not count `HEAD`s against the limit (measured: the remaining
allowance stayed at 100 across `HEAD`s and dropped on one `GET`), so the sweep does not spend
the budget it would trip over, and nothing needs forgiving.

```bash
sh scripts/check_pinned_images.sh
# sha256:fb27b00a... pgvector/pgvector:pg14
# sha256:4ea9e5ed... postgres:14-alpine
# sha256:729bf65c... postgrest/postgrest:v12.2.3
# ...
# sha256:1b1d9202... registry.gitlab.syncad.com/hive/haf:1.29.0-rc2
# ...
# [check-pinned-images] positively resolved 12 of 12: 0 missing or unreadable, 0 unanswered
```

Needs `docker` (the CLI with the compose plugin, for `docker compose config`), `curl` and
`jq`. It needs **no docker daemon** and **no login**: these are public repositories, and
authenticating would keep the check green for a repository that has become unreadable to
anyone without our credentials. It gives the whole sweep a budget
(`HAF_FYP_RESOLVE_DEADLINE`, 420s) that the CI job's timeout sits above, so a hung registry
ends in the script's own account of what it proved rather than a bare job timeout.

It runs compose in an **emptied environment** (`env -i`, keeping only `PATH` and `HOME`).
Compose reads the shell environment in preference to `--env-file` (`docker/.env` says so
under `HAF_VERSION`), so otherwise a CI/CD variable named `HAF_VERSION` — or the documented
`HAF_FYP_REWRITER_IMAGE` override, which `docker/.env` does not even define — would silently
redirect the check away from the pin the repository actually ships.

Nothing else notices a dead pin. `compose-validate` runs `docker compose config`, which
parses YAML and never contacts a registry — and deliberately uses placeholder values, since
a lane that cannot pull cannot tell a good reference from a dead one. `docker-compose-ci.yml`
runs plain PostgreSQL with pgvector, so no test lane pulls HAF at all. The guards in
`tests/unit/test_compose_contract.py` are offline by design: they keep the reference in the
shape that resolved when it was last run. That combination is how
[#56](https://gitlab.syncad.com/hive/haf_fyp/-/issues/56) lived — the stack asked for an
image path nobody had ever published, and every pipeline stayed green until an operator hit
it on a cold cache.

### The scheduled lane

CI runs it as `registry-resolution`, **on a schedule, or when asked for** — never in a push
or merge-request pipeline ([#71](https://gitlab.syncad.com/hive/haf_fyp/-/issues/71)). Whether
a tag still exists is a question whose answer changes without anyone touching this repository,
so a merge request is the wrong moment to ask it and the wrong thing to block on a registry
being reachable. It has no `allow_failure` of any kind, and it is not interruptible —
otherwise a push to the default branch just after the tick cancels the sweep with no verdict.

Create it under **Build → Pipeline schedules** (`/-/pipeline_schedules`) on the default
branch; a daily or weekly cadence is plenty. To run it without waiting for a tick, run a
pipeline with `RUN_REGISTRY_RESOLUTION=true` — that pipeline runs the sweep and nothing else.

Worth knowing before creating it: a scheduled pipeline is a pipeline for the default branch's
head, so it shows in that branch's status like any other. A red sweep turns `develop` red
until the next push, and a green one can sit on top of a red push pipeline; its failure mail
goes to the schedule's owner.

Nothing else in `.gitlab-ci.yml` runs in a scheduled pipeline — every other lane carries
`.standard-rules`, whose first rule is `$CI_PIPELINE_SOURCE == "schedule" → when: never`, and
`publish_tag` carries the same exclusion of its own because a schedule may target a **tag**,
where it would otherwise re-publish a release from unchanged source on a timer. That rule is
load-bearing rather than tidy: a scheduled pipeline runs *on a branch*, so it sets `CI_COMMIT_BRANCH`, and a schedule on `develop` would
otherwise match every lane's `$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH` — running the three
`docker-build` jobs on a timer, republishing `:<short-sha>` and `:develop` from unchanged
source, and moving the very digests this lane exists to watch.

The job prints the **manifest-list digest** next to each reference, which is what
`docker pull` records. A tag that is re-pointed upstream still resolves — `docker/.env`
records `latest` sharing a digest with a develop build while the registry still reports it
created months earlier — so the digest is what makes a re-point visible as a change between
runs rather than another green tick.

## Running Tests

```bash
# PostgREST must be running
# This suite drives the raw /rpc/ surface, so point it at PostgREST (3000), not the
# rewriter (3001). tests/tavern/rewriter/ is the suite that exercises the clean paths.
API_URL=http://localhost:3000 tavern-ci tests/tavern/test_endpoints.tavern.yaml
```

### The install lane

`install-test` in CI runs the installer and the uninstaller for real
([#47](https://gitlab.syncad.com/hive/haf_fyp/-/issues/47)), from the SQL image, as
`haf_admin`, on PostgreSQL 17:

```bash
docker compose -f docker/docker-compose-ci-install.yml up --detach --build
docker compose -f docker/docker-compose-ci-install.yml wait verify-gone   # 0 = the whole lane passed
docker compose -f docker/docker-compose-ci-install.yml logs verify verify-locked verify-again verify-gone
docker compose -f docker/docker-compose-ci-install.yml down -v
```

| step | what it does |
|---|---|
| `install` | `install_app` on a database that has never seen haf_fyp, with one other application on it |
| `verify` | `tests/ci_install_verify.sql`: the roles and what they are members of; ownership; every table and function that must exist, by name; the HAF context and its registration; the one-row tables; the recorded version; what the API role may read, execute and write; the default privileges; the other application untouched |
| `age` | `tests/ci_install_age.sql` changes what a running node changes: the sync pointer, the active algorithm version, a user's declared interests |
| `lock-holder`, `install-locked`, `verify-locked` | with a block processor's lock held, `install_app` exits 0 and changes nothing |
| `release-lock`, `install-again`, `verify-again` | with the lock released, `install_app` runs over that database: everything the installer creates is identical to what `verify` recorded (function bodies and grants included), and everything `age` changed is still as `age` left it |
| `uninstall` | `uninstall_app --force` |
| `verify-gone` | `tests/ci_uninstall_verify.sql`: nothing of haf_fyp is left, and pgvector, HAF's roles and grants and the other application are |

`tests/unit/test_install_lane.py` states the compose file and the CI job in full and
compares, so a change to either has to be made there too.

**What it does not cover.**

- The database is PostgreSQL with pgvector, `tests/haf_stub.sql` and
  `tests/ci_install_setup.sql`, which together model the HAF application API. It is not
  HAF: a change in HAF itself does not turn this lane red.
- No block is processed. The processors read HAF's operation tables, which the model
  does not have; that they all exist is checked, that they work is not.
- All three installs are the same commit, so a defect that only an upgrade from an older
  commit shows is not seen.
- No other application's schema is present, so the parts of `db/cross_schema_grants.sql`
  that grant on hivemind, HiveSense, reputation_tracker and balance_tracker only print
  that they found nothing. `scripts/grant_cross_schema.sh` is not run.
- The installer's refusals (a context owned by someone else, pgvector out of reach) and
  the uninstaller's confirmation prompt are not exercised; the lane only takes the path
  where everything is in order, and `--force`.
- `--swagger-url` is not passed.

## SQL Linting

```bash
sqlfluff lint db/ endpoints/ --dialect postgres
```

The CI pipeline runs this automatically on merge requests.
