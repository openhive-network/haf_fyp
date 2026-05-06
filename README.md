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
| `haf_fyp.api` | FastAPI overlay (rate limiting, force-refresh) |
| PostgREST | Auto-generated REST API |
| Redis | Pub/sub for on-demand feed refresh |

## Prerequisites

- A running HAF node (`haf_block_log` PostgreSQL database)
- `pgvector` extension installed on the HAF PostgreSQL server:
  ```sql
  -- run as superuser
  CREATE EXTENSION IF NOT EXISTS vector;
  ```
- Docker and Docker Compose

## Quick Start

```bash
# 1. Copy and configure environment
cp docker/.env docker/.env.local
# Edit .env.local: set HAF_VERSION, pin registries for production

# 2. Install schema (one-time)
docker compose -f docker/docker-compose.yml up haf-fyp-setup

# 3. Start the full stack
docker compose -f docker/docker-compose.yml up haf-fyp-sync haf-fyp-ranker haf-fyp-api postgrest redis

# 4. Smoke test
curl http://localhost:3001/rpc/get_health
curl http://localhost:3001/rpc/get_algorithm
```

## API Endpoints

All read endpoints are served by PostgREST on port 3001. The FastAPI overlay on port 3002 handles write operations and advanced features.

### PostgREST (port 3001)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/rpc/get_health` | App status and last processed block |
| GET | `/rpc/get_algorithm` | Active algorithm version and weights |
| GET | `/rpc/get_personalized_feed?username=alice&limit=20` | Ranked feed for a user |
| GET | `/rpc/get_global_feed?limit=20` | Unauthenticated global feed |
| GET | `/rpc/get_user_profile?username=alice` | User vector status and top communities |

### FastAPI overlay (port 3002)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Service health check |

Clean URL rewrites (via nginx `endpoints/rewrite_rules.conf`):

```
/v1/fyp/feed/{username}     →  /rpc/get_personalized_feed
/v1/fyp/global              →  /rpc/get_global_feed
/v1/fyp/profile/{username}  →  /rpc/get_user_profile
/v1/fyp/algorithm           →  /rpc/get_algorithm
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
| `relevance` | [0, 1] | Cosine similarity: post embedding vs user interest vector (HiveSense) |
| `recency` | [0, 1] | `exp(−ln2 × age_hours / 24)` — 24-hour half-life |
| `engagement` | [0, 1] | Sigmoid-normalised unique commenters + reblogs + net rshares |
| `credibility` | [0, 1] | Author reputation from btracker + reputation_tracker |
| `community_boost` | ×1.20 | Applied post-sum for on-chain subscribed community posts |

All weights are stored in `haf_fyp_app.algorithm_versions` and governed by a community RFC process. The active version is returned by `/rpc/get_algorithm`. Every response includes per-signal component scores for full transparency.

Cold start: for new users without an interest vector, relevance is dropped and the remaining three weights are renormalised to sum to 1.0. Existing on-chain community subscriptions are used to bootstrap the interest vector immediately.

## Database Schema

```
haf_fyp_app
├── post_candidates       — root posts eligible for ranking
├── post_lookup           — (author, permlink) index for processor joins
├── post_commenters       — unique commenter pairs per post
├── engagement_metrics    — comment/reblog/vote counts, net rshares
├── user_profile_vectors  — 768-dim interest vector per user (pgvector)
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
haf-fyp-setup    # one-shot DDL install (Dockerfile.sql)
haf-fyp-sync     # PL/pgSQL block loop via process_blocks.sh (Dockerfile.sql)
haf-fyp-ranker   # Python ranking daemon, 5-minute cycles (Dockerfile.python)
haf-fyp-api      # FastAPI overlay on port 3002 (Dockerfile.python)
postgrest        # PostgREST on port 3001
redis            # pub/sub for force-refresh signals
```

## Development Phases

| Phase | What |
|-------|------|
| **1 — Skeleton** ✅ | Schema, tables, stubs, Docker stack |
| **2 — Post ingestion** | `process_posts.sql` — ingest root posts, post_lookup, engagement init |
| **3 — Engagement tracking** | `process_engagement.sql` — comments, reblogs, votes, payout windows |
| **4 — Community sync** | `process_communities.sql` — subscribe/unsubscribe from custom_json |
| **5 — Python ranker** | `scoring.py` — recency + engagement scoring, feed_cache writes |
| **6 — HiveSense** | Semantic relevance via pgvector embeddings |
| **7 — Credibility** | btracker + reputation_tracker cross-schema reads |
| **8 — Cold start** | Interest picker, POST /v1/fyp/interests, vector bootstrap |
| **9 — Production** | Rate limiting, score explainer, algorithm governance |

## Verification Tasks (pre-deployment)

Before connecting to a live HAF node, confirm these against a running instance:

- **T-0.3** — `hive.app_next_iteration` INOUT parameter signature
- **T-0.7** — `custom_json_operation` community subscribe/unsubscribe body format
- **T-0.1/T-0.5** — HiveSense `post_embeddings` table schema and vector dimension

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
