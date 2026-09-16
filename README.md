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

- A running HAF node (`haf_block_log` PostgreSQL database)
- `pgvector` extension installed on the HAF PostgreSQL server:
  ```sql
  -- run as superuser before installing haf_fyp
  CREATE EXTENSION IF NOT EXISTS vector;
  ```
- Docker and Docker Compose
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
docker compose -f docker/docker-compose.yml run --rm haf-fyp-setup \
  /app/scripts/grant_cross_schema.sh
```

## Quick Start

```bash
# 1. Copy and configure environment
cp docker/.env docker/.env.local
# Edit .env.local:
#   - Set HAF_VERSION to a pinned release tag (not "latest") for production
#   - Set FYP_ADMIN_API_KEY to a strong random secret
#   - Set TRUSTED_PROXY_IPS to whatever fronts this app (Caddy/Varnish/HAProxy),
#     or to `direct` if nothing does. Left empty, every caller shares one bucket
#     and pre-validation rate limiting stays OFF -- see the deployment checklist.
#
# Every command below passes --env-file docker/.env.local. Without it Compose reads
# docker/.env (the project-directory default) and the edits above are silently ignored,
# leaving FYP_ADMIN_API_KEY at its shipped placeholder.

# 2. Install schema (one-time)
#    GIT_HASH is what GET /version reports. --short=8 matches CI's CI_COMMIT_SHORT_SHA,
#    so the hash is the same width however the node was installed. Omit it and /version
#    reports a placeholder rather than a commit.
#    --build is required: GIT_HASH is a build argument baked into the image, so a plain
#    `up` (or `run`) reuses the existing image and silently ignores a changed export.
#    It is baked rather than passed at runtime on purpose -- the image carries the SQL,
#    so the hash must describe the code inside it. See Dockerfile.sql.
export GIT_HASH="$(git rev-parse --short=8 HEAD)"
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

> **Upgrading from before #48:** the `postgrest` service is now `haf-fyp-postgrest`.
> Renaming a compose service does not rename its running container: the old one is
> orphaned and keeps host port 3000, so a plain quick-start step 3 fails with `port is
> already allocated` and leaves the new PostgREST and the rewriter half-created. Stopping
> the renamed services does not reach the orphan, and neither does a plain `down` — so it
> can also keep PostgREST's sessions open as `haf_fyp_user`, and the uninstall script
> refuses (the uninstall command below passes `--remove-orphans` for that reason).
>
> 1. If `docker/.env.local` sets `POSTGREST_HOST=postgrest`, delete that line.
> 2. Remove the two containers the rename affects. This matters most after a failed step
>    3: retrying `up` over its half-created PostgREST exits 0 but starts it with **no
>    network**, so the rewriter cannot resolve it. It also forces the rewriter to be
>    recreated, and so to resolve its upstream afresh:
>
>    ```bash
>    docker compose --env-file docker/.env.local -f docker/docker-compose.yml \
>      rm -sf haf-fyp-postgrest haf-fyp-postgrest-rewriter
>    ```
>
> 3. Remove the orphan and start the stack, with quick-start step 3's service list (add
>    `--build` if the same pull changed anything else — see quick-start step 2). Like
>    step 3, this re-runs `haf-fyp-setup`, and PostgREST starts only once it finishes:
>
>    ```bash
>    docker compose --env-file docker/.env.local -f docker/docker-compose.yml up -d \
>      --remove-orphans haf-fyp-sync haf-fyp-ranker haf-fyp-api haf-fyp-postgrest \
>      haf-fyp-postgrest-rewriter haf-fyp-redis
>    ```
>
> 4. Smoke-test through the rewriter. `/health` on 3001 goes to PostgREST
>    (`/rpc/get_health`), and `-f` makes an error status a non-zero exit:
>
>    ```bash
>    curl -fsS http://localhost:3001/health
>    ```

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
| GET | `/health` | Service health check |
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
correct key can never be locked out by guessers. Per-IP keying depends on `TRUSTED_PROXY_IPS` being set correctly; see the
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
                   # starts on a plain `up` (Dockerfile.sql)
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

### The SQL image's verbs

`Dockerfile.sql` builds one image used by three services, dispatched by
`docker/docker_entrypoint.sh`:

| Verb | Runs | Notes |
|------|------|-------|
| `install_app [--swagger-url=HOST] [--host=] [--port=] [--user=] [--is_forking=FALSE]` | `scripts/install_app.sh` | idempotent; safe to re-run |
| `process_blocks` | `scripts/process_blocks.sh` | infinite loop |
| `uninstall_app [--force] [--host=] [--port=] [--user=]` | `scripts/uninstall_app.sh` | destructive |

This is the trio every haf_api_node app image exposes, which is what lets a module file
say `command: ["install_app"]` rather than reaching into the image for a script path.

Connection settings default from the environment (`POSTGRES_HOST`, `POSTGRES_PORT`,
`POSTGRES_DB`, `POSTGRES_USER`); `--host=` / `--port=` / `--user=` override them, and
the entrypoint forwards them exactly as balance_tracker's does, so a runbook copied
from an upstream app works here. An argument neither option loop recognises is now an
error rather than being silently discarded — which also turns a mistyped
`--swagger-url` from a silent fallback to `localhost` into a failed install.

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

```bash
docker compose --env-file docker/.env.local -f docker/docker-compose.yml \
  stop haf-fyp-sync haf-fyp-ranker haf-fyp-api haf-fyp-postgrest
docker compose --env-file docker/.env.local -f docker/docker-compose.yml \
  run --rm --remove-orphans haf-fyp-uninstall
```

`--remove-orphans` removes containers of services no longer in the file before the
uninstall starts — on a stack upgraded across #48, the old `postgrest`, which `stop`
cannot reach and which would otherwise hold sessions the script refuses on.

The profile keeps it out of a plain `up`, and is namespaced `haf-fyp-uninstall` so
`COMPOSE_PROFILES` cannot collide with another app's teardown on a shared node.

For a confirmation prompt, run `scripts/uninstall_app.sh` directly without `--force`:
it asks you to type the database name, and refuses outright if there is no TTY to ask
on.

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
- [ ] `HAF_VERSION` pinned to a specific release tag (not `latest`)
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
| `RATELIMIT_SWALLOW_ERRORS=1` | Only affects slowapi's own paths; this app already fails **open** on a limiter-storage error by design (see below), so it changes little here. |

**Failure policy: the limiter fails open.** If Redis is unreachable or slow, a request
is served *unmetered* rather than rejected — limits protect against abuse, they are not
part of answering correctly, so a Redis incident degrades limiting instead of the
service. Storage failures are logged once per minute with a suppressed-occurrence
count, and again immediately if a *different* kind of failure appears.

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
so use `up -d --build haf-fyp-api`, not a bare `up -d`, which silently reuses the cached
image. `haf-fyp-ranker` builds from the same `Dockerfile.python`, so a plain
`docker compose build` restarts it mid-cycle — rebuild the api service by name if that
matters. Nothing needs ordering and a rollback strands nothing: bucket identities are
unchanged and every key expires with its window.

⚠️ **The upgrade that renamed `redis` to `haf-fyp-redis` is the exception to every
sentence above.** Recreate `haf-fyp-api` **and** `haf-fyp-ranker` together, and pass
`--remove-orphans`. Renaming a compose service does not rename the running container:
the old one is orphaned and keeps the `redis` network alias, so a per-service
`up -d --build haf-fyp-api` leaves the ranker resolving `redis` while the api publishes
to `haf-fyp-redis`. Force-refresh then stops working **with no error on either side**
— the publish succeeds into an instance nobody subscribes to, and its
`except Exception: pass` would swallow a failure anyway — until the ranker's next
5-minute cycle picks the work up regardless. The orphan also still mounts
`redis_data`, so two `redis-server` processes share one `/data` until it is removed.

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

## Running Tests

```bash
# PostgREST must be running
# This suite drives the raw /rpc/ surface, so point it at PostgREST (3000), not the
# rewriter (3001). tests/tavern/rewriter/ is the suite that exercises the clean paths.
API_URL=http://localhost:3000 tavern-ci tests/tavern/test_endpoints.tavern.yaml
```

## SQL Linting

```bash
sqlfluff lint db/ endpoints/ --dialect postgres
```

The CI pipeline runs this automatically on merge requests.
