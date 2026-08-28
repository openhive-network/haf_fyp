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

Every feed entry carries a nested `fyp.source` field — `personalized` when the user has a precomputed personalized feed, or `global` when `get_personalized_feed` serves the cold-start global fallback (it is uniform across all entries in one response). A client can therefore tell directly from a feed call whether the user is getting their own ranked feed or the global fallback, without a separate `get_user_profile` lookup (`has_feed`).
| GET | `/rpc/get_user_profile?username=alice` | User vector status and top communities |
| GET | `/rpc/get_score_explanation?username=alice&post_id=123` | Per-signal score breakdown |

### FastAPI overlay (port 3002)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Service health check |
| GET | `/metrics` | Embedding coverage (#15) + credibility source health (#8) |
| GET | `/sync-status` | Last block processed by the `haf_fyp` HAF context + its UTC timestamp (#16) |
| POST | `/v1/fyp/interests` | Declare cold-start community interests, signed (10 req/min) |
| DELETE | `/v1/fyp/interests` | Un-opt-in: clear declared interests, signed (5 req/min) |
| POST | `/v1/fyp/events` | Impression / engagement telemetry batch (120 req/min) |
| POST | `/v1/fyp/admin/algorithm` | Activate an algorithm version (`X-Admin-Key` required) |

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

Clean URL rewrites (via nginx `endpoints/rewrite_rules.conf`):

```
/v1/fyp/feed/{username}              →  /rpc/get_personalized_feed
/v1/fyp/global                       →  /rpc/get_global_feed
/v1/fyp/profile/{username}           →  /rpc/get_user_profile
/v1/fyp/algorithm                    →  /rpc/get_algorithm
/v1/fyp/algorithms                   →  /rpc/list_algorithm_versions
/v1/fyp/explain/{username}/{post_id} →  /rpc/get_score_explanation
```

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
haf-fyp-setup    # one-shot DDL install + cross-schema grants (Dockerfile.sql)
haf-fyp-sync     # PL/pgSQL block loop via process_blocks.sh (Dockerfile.sql)
haf-fyp-ranker   # Python ranking daemon, 5-minute cycles (Dockerfile.python)
haf-fyp-api      # FastAPI overlay on port 3002 (Dockerfile.python)
postgrest        # PostgREST on port 3001
redis            # pub/sub for force-refresh signals
```

## Deployment Checklist

Before going live:

- [ ] `pgvector` extension installed by superuser on the HAF PostgreSQL server
- [ ] `FYP_ADMIN_API_KEY` set to a strong random value in `docker/.env.local`
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
