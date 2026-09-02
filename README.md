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
                    PostgREST (port 3001) + FastAPI (port 3002)
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
| Redis | Pub/sub for on-demand feed refresh |

## Prerequisites

### Required

- A running HAF node (`haf_block_log` PostgreSQL database)
- `pgvector` extension installed on the HAF PostgreSQL server:
  ```sql
  -- run as superuser before installing haf_fyp
  CREATE EXTENSION IF NOT EXISTS vector;
  ```
- Docker and Docker Compose

### External HAF Apps (for full scoring accuracy)

The ranker degrades gracefully without these — relevance defaults to 0.5, and credibility is computed from whichever source is available with the weights renormalised — but full accuracy requires all of them installed and synced on the same PostgreSQL server:

| App | Provides | Degrades to |
|-----|----------|-------------|
| [HiveSense](https://gitlab.syncad.com/hive/hivesense) | 1024-dim post embeddings (`hivesense_app.posts_vectors`, halfvec; keyed by hivemind post id) | relevance = 0.5 neutral |
| [Hivemind](https://gitlab.syncad.com/hive/hivemind) | `(author, permlink)` → hivemind post id, the bridge to HiveSense embeddings (`hivemind_app.hive_posts`) | relevance = 0.5 neutral |
| [reputation_tracker](https://gitlab.syncad.com/hive/reputation_tracker) | Per-account reputation scores (`reptracker_app.account_reputations`) | credibility from VESTS alone, renormalised |
| [balance_tracker](https://gitlab.syncad.com/hive/balance_tracker) | Per-account VESTS stake (`btracker_app.current_account_balances`, or `btracker_backend` / `hafbe_bal` under the block explorer) | credibility from reputation alone, renormalised |

With NEITHER credibility source readable, every author scores the 0.3 default. A
missing component is dropped from the weighted average rather than scored 0 at full
weight (#8) — zeroing it compressed every author's credibility into `[0, 0.6]` and
shifted the ranking, so a missing tracker changed the ORDER of the feed rather than
just widening it.

`GET /metrics` reports each source as `readable` / `unreadable` / `absent`.
`unreadable` means the relation exists but this deployment has lost `SELECT` on it —
a repairable misconfiguration, fixed by re-running `scripts/grant_cross_schema.sh`.
`absent` just means the app is not installed here. The distinction matters: those two
used to be indistinguishable, so a lost grant silently degraded ranking quality with
nothing to notice.

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
  haf-fyp-sync haf-fyp-ranker haf-fyp-api postgrest redis

# 4. Smoke test
curl http://localhost:3001/rpc/get_health
curl http://localhost:3001/rpc/get_algorithm

# 5. After installing HiveSense / btracker / reptracker — grant cross-schema access
docker compose --env-file docker/.env.local -f docker/docker-compose.yml run --rm haf-fyp-setup \
  /app/scripts/grant_cross_schema.sh
```

## API Endpoints

All read endpoints are served by PostgREST on port 3001. The FastAPI overlay on port 3002 handles write operations and admin actions.

### PostgREST (port 3001)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/rpc/get_health` | App status and last processed block |
| GET | `/rpc/get_algorithm` | Active algorithm version and weights |
| GET | `/rpc/list_algorithm_versions` | All registered algorithm versions |
| GET | `/rpc/get_personalized_feed?username=alice&limit=20` | Ranked feed for a user |
| GET | `/rpc/get_global_feed?limit=20` | Unauthenticated global feed |
| GET | `/rpc/get_user_profile?username=alice` | User vector status and top communities |
| GET | `/rpc/get_score_explanation?username=alice&post_id=123` | Per-signal score breakdown |
| GET | `/rpc/get_feed_metrics` | Feed performance analytics per algorithm version (#13) — product metrics, not service health |

Every feed entry carries a nested `fyp.source` field — `personalized` when the user has a precomputed personalized feed, or `global` when `get_personalized_feed` serves the cold-start global fallback (it is uniform across all entries in one response). A client can therefore tell directly from a feed call whether the user is getting their own ranked feed or the global fallback, without a separate `get_user_profile` lookup (`has_feed`).

### FastAPI overlay (port 3002)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Service health check |
| GET | `/metrics` | Operational health: embedding coverage (#15) + credibility source health (#8) — not feed analytics |
| GET | `/sync-status` | Last block processed by the `haf_fyp` HAF context + its UTC timestamp (#16) |
| POST | `/v1/fyp/interests` | Declare cold-start community interests, signed (10/min per IP, 60/min total) |
| DELETE | `/v1/fyp/interests` | Un-opt-in: clear declared interests, signed (5/min per IP, 30/min total) |
| POST | `/v1/fyp/events` | Impression / engagement telemetry batch (120/min per IP, 600/min total) |
| POST | `/v1/fyp/admin/algorithm` | Activate an algorithm version, `X-Admin-Key` required (5/min per IP; 30/min total, charged only on a wrong key) |

The two metrics endpoints are unrelated and answer different questions. This one, on the
FastAPI overlay, is the *operational* read — embedding coverage and credibility source
health — and is what an embedding-gap or lost-grant alert should watch. It carries no block
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

It is served **only on the FastAPI overlay (port 3002)**, per #16. haf_api_node's stock
agent checks probe `http://<app>-postgrest-rewriter:80/sync-status`, which haf_fyp does
not serve, so a haf_api_node profile must point its `*_HEALTH_URL` at port 3002 — or
#17 should add a PostgREST twin so the standard probe URL works.

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
postgrest          # PostgREST on port 3001
redis              # pub/sub for force-refresh signals
```

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
  stop haf-fyp-sync haf-fyp-ranker haf-fyp-api postgrest
docker compose --env-file docker/.env.local -f docker/docker-compose.yml \
  run --rm haf-fyp-uninstall
```

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
- [ ] **`TRUSTED_PROXY_IPS` set to the reverse proxy's address or CIDR** if anything fronts this app. Rate limits key on `X-Real-IP` only from a trusted peer; unset, every caller keys on the proxy's address instead — one shared bucket for all of them, which is no per-client limit at all. The app logs a warning at startup when it is unset, and once more if a peer outside the list presents the header (which usually means the proxy was recreated with a new address and the list is now stale). Re-check it after anything that can renumber the proxy container.
- [ ] `HAF_VERSION` pinned to a specific release tag (not `latest`)
- [ ] HiveSense installed and synced → `grant_cross_schema.sh` re-run
- [ ] Hivemind installed and synced (provides the (author, permlink) → hivemind post id bridge) → `grant_cross_schema.sh` re-run
- [ ] balance_tracker installed and synced → `grant_cross_schema.sh` re-run
- [ ] reputation_tracker installed and synced → `grant_cross_schema.sh` re-run
- [ ] Verify `hivesense_app.posts_vectors` on live instance: `\d hivesense_app.posts_vectors` (expected: `embedding halfvec(1024)`, keyed by hivemind post id, one row per chunk)
- [ ] Smoke test all endpoints after first ranker cycle completes

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
| T-0.3 — HAF sync loop API | ✅ Resolved | Correct function is `hive.app_next_block()` returning `hive.blocks_range`; `hive.app_next_iteration` does not exist |
| T-0.7 — community custom_json format | ✅ Resolved | Double-encoded JSON array `["subscribe", {"community": "hive-NNNNNN"}]`; account from `required_posting_auths[0]` |
| T-0.1 — HiveSense table and column names | ✅ Resolved | Live: `hivesense_app.posts_vectors.embedding halfvec(1024)`, keyed by **hivemind post id** (`hive_posts.id`), one row per chunk. haf_fyp's own post_id keyspace does not overlap, so the ranker bridges via (author, permlink) → hivemind → hivesense and averages chunks |

## Running Tests

```bash
# PostgREST must be running
API_URL=http://localhost:3001 tavern-ci tests/tavern/test_endpoints.tavern.yaml
```

## SQL Linting

```bash
sqlfluff lint db/ endpoints/ --dialect postgres
```

The CI pipeline runs this automatically on merge requests.
