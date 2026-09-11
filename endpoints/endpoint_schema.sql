SET ROLE haf_fyp_owner;

/** openapi
openapi: 3.1.0
info:
  title: HAF FYP - For You Page
  description: >-
    Personalized content feed API for the Hive blockchain.
    Provides algorithmically ranked post feeds, user profiles,
    and algorithm version management.
  license:
    name: MIT License
    url: https://opensource.org/license/mit
  version: 3.0.0
externalDocs:
  description: HAF FYP GitLab repository
  url: https://gitlab.syncad.com/hive/haf_fyp
tags:
  - name: Feed
    description: Personalized and global content feeds
  - name: Algorithm
    description: Algorithm version registry
  - name: User
    description: User profile information
  - name: Other
    description: Health and version endpoints
servers:
  - url: /haf-fyp-api
 */

/** openapi:components
schemas:
  fyp_ranking:
    type: object
    x-sql-datatype: object
    description: This app's ranking data for the post (the FYP-specific signal).
    properties:
      source:
        type: string
        enum: [personalized, global]
        description: >-
          Which feed produced this entry - 'personalized' when the user has a
          precomputed personalized feed, 'global' when served the cold-start
          global fallback. Uniform across all entries in a single response.
      rank:
        type: integer
        description: Position in the feed (1-based)
      final_score:
        type: number
        description: Composite ranking score [0, 1]
      score_relevance:
        type: number
        nullable: true
        description: Semantic relevance to the user's interest vector (null in the global feed)
      score_recency:
        type: number
      score_engagement:
        type: number
      score_credibility:
        type: number
      community_boost_applied:
        type: boolean
      boost_source:
        type: string
        nullable: true
        description: Which tier applied the community boost - 'subscribed', 'engaged', or null
      post_id:
        type: string
        description: >-
          Internal post id (joins feed_events.post_id). Serialized as a STRING
          because the id exceeds JavaScript's 2^53 safe-integer limit - a JS
          Number would lose precision and break the telemetry join; echo it back
          verbatim in POST /v1/fyp/events.
      algorithm_version:
        type: string
        nullable: true
        description: Ranking algorithm version that produced this entry, for A/B analysis. Global-sourced entries carry it too (#23); null only for entries written before that change.
 */

/** openapi:components
schemas:
  feed_entry:
    type: object
    x-sql-datatype: object
    description: >-
      A ranked post. The post itself is returned in the exact shape produced by
      Hivemind's bridge.get_ranked_posts (author, permlink, title, body,
      json_metadata, category, created/updated, payout / pending_payout /
      is_paidout / payout_at, author_reputation, author_role/title, children,
      net_rshares, active_votes, stats, beneficiaries, percent_hbd,
      max_accepted_payout, community / community_title, url, blacklists, reblogs,
      ...), so any front end that renders trending / hot / new can render this
      feed unchanged. A nested "fyp" object adds this app's ranking data.
    properties:
      author:
        type: string
      permlink:
        type: string
      title:
        type: string
      body:
        type: string
      category:
        type: string
      community:
        type: string
        nullable: true
      created:
        type: string
        format: date-time
      payout:
        type: number
      author_reputation:
        type: number
      children:
        type: integer
      json_metadata:
        type: object
      active_votes:
        type: array
        items:
          type: object
      stats:
        type: object
      url:
        type: string
      fyp:
        $ref: '#/components/schemas/fyp_ranking'
    example:
      author: cryptoandcoffee
      permlink: will-we-see-the-hydration-break-next-season
      category: hive-115814
      title: Will We See The Hydration / Advertising Break In The Premier League Next Season?
      body: "<center><img src=\"https://images.hive.blog/p/cover.png\"/></center>\n\nMarkdown body of the post..."
      json_metadata:
        app: leothreads/0.3
        format: markdown
        tags: [sports, football, premierleague, advertising]
        image: ["https://images.hive.blog/p/cover.png"]
      created: "2026-06-19T16:33:57"
      updated: "2026-06-19T16:33:57"
      depth: 0
      children: 4
      community: hive-115814
      community_title: Sportsblock
      url: /hive-115814/@cryptoandcoffee/will-we-see-the-hydration-break-next-season
      author_reputation: 84.5
      author_role: guest
      author_title: ""
      payout: 3.655
      pending_payout_value: 3.655 HBD
      author_payout_value: 0.000 HBD
      curator_payout_value: 0.000 HBD
      max_accepted_payout: 1000000.000 HBD
      percent_hbd: 10000
      is_paidout: false
      payout_at: "2026-06-26T16:33:57"
      net_rshares: 50938299772643
      beneficiaries:
        - account: leo.voter
          weight: 500
      blacklists: []
      reblogs: 0
      replies: []
      active_votes:
        - voter: team
          rshares: 30982500342
        - voter: roelandp
          rshares: 15794227518
      stats:
        hide: false
        gray: false
        total_votes: 127
        flag_weight: 0.0
      fyp:
        source: personalized
        rank: 1
        final_score: 0.9178
        score_relevance: 0.62
        score_recency: 0.9926
        score_engagement: 0.9805
        score_credibility: 0.5679
        community_boost_applied: true
        boost_source: subscribed
 */

-- ---------------------------------------------------------------------------
-- root() - serves the full OpenAPI spec at GET /
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_endpoints.root() RETURNS json
LANGUAGE plpgsql STABLE
AS $func$
DECLARE
    -- The OpenAPI spec below is generated from the /** openapi ... */ annotations
    -- in this file by scripts/openapi_rewrite.sh (it fills the block after the
    -- "-- openapi-spec" marker). Edit the annotations, not the generated JSON.
-- openapi-spec
-- openapi-generated-code-begin
  openapi json = $$
{
  "openapi": "3.1.0",
  "info": {
    "title": "HAF FYP - For You Page",
    "description": "Personalized content feed API for the Hive blockchain. Provides algorithmically ranked post feeds, user profiles, and algorithm version management.",
    "license": {
      "name": "MIT License",
      "url": "https://opensource.org/license/mit"
    },
    "version": "3.0.0"
  },
  "externalDocs": {
    "description": "HAF FYP GitLab repository",
    "url": "https://gitlab.syncad.com/hive/haf_fyp"
  },
  "tags": [
    {
      "name": "Feed",
      "description": "Personalized and global content feeds"
    },
    {
      "name": "Algorithm",
      "description": "Algorithm version registry"
    },
    {
      "name": "User",
      "description": "User profile information"
    },
    {
      "name": "Other",
      "description": "Health and version endpoints"
    }
  ],
  "servers": [
    {
      "url": "/haf-fyp-api"
    }
  ],
  "components": {
    "schemas": {
      "fyp_ranking": {
        "type": "object",
        "x-sql-datatype": "object",
        "description": "This app's ranking data for the post (the FYP-specific signal).",
        "properties": {
          "source": {
            "type": "string",
            "enum": [
              "personalized",
              "global"
            ],
            "description": "Which feed produced this entry - 'personalized' when the user has a precomputed personalized feed, 'global' when served the cold-start global fallback. Uniform across all entries in a single response."
          },
          "rank": {
            "type": "integer",
            "description": "Position in the feed (1-based)"
          },
          "final_score": {
            "type": "number",
            "description": "Composite ranking score [0, 1]"
          },
          "score_relevance": {
            "type": "number",
            "nullable": true,
            "description": "Semantic relevance to the user's interest vector (null in the global feed)"
          },
          "score_recency": {
            "type": "number"
          },
          "score_engagement": {
            "type": "number"
          },
          "score_credibility": {
            "type": "number"
          },
          "community_boost_applied": {
            "type": "boolean"
          },
          "boost_source": {
            "type": "string",
            "nullable": true,
            "description": "Which tier applied the community boost - 'subscribed', 'engaged', or null"
          },
          "post_id": {
            "type": "string",
            "description": "Internal post id (joins feed_events.post_id). Serialized as a STRING because the id exceeds JavaScript's 2^53 safe-integer limit - a JS Number would lose precision and break the telemetry join; echo it back verbatim in POST /v1/fyp/events."
          },
          "algorithm_version": {
            "type": "string",
            "nullable": true,
            "description": "Ranking algorithm version that produced this entry, for A/B analysis. Global-sourced entries carry it too (#23); null only for entries written before that change."
          }
        }
      },
      "feed_entry": {
        "type": "object",
        "x-sql-datatype": "object",
        "description": "A ranked post. The post itself is returned in the exact shape produced by Hivemind's bridge.get_ranked_posts (author, permlink, title, body, json_metadata, category, created/updated, payout / pending_payout / is_paidout / payout_at, author_reputation, author_role/title, children, net_rshares, active_votes, stats, beneficiaries, percent_hbd, max_accepted_payout, community / community_title, url, blacklists, reblogs, ...), so any front end that renders trending / hot / new can render this feed unchanged. A nested \"fyp\" object adds this app's ranking data.",
        "properties": {
          "author": {
            "type": "string"
          },
          "permlink": {
            "type": "string"
          },
          "title": {
            "type": "string"
          },
          "body": {
            "type": "string"
          },
          "category": {
            "type": "string"
          },
          "community": {
            "type": "string",
            "nullable": true
          },
          "created": {
            "type": "string",
            "format": "date-time"
          },
          "payout": {
            "type": "number"
          },
          "author_reputation": {
            "type": "number"
          },
          "children": {
            "type": "integer"
          },
          "json_metadata": {
            "type": "object"
          },
          "active_votes": {
            "type": "array",
            "items": {
              "type": "object"
            }
          },
          "stats": {
            "type": "object"
          },
          "url": {
            "type": "string"
          },
          "fyp": {
            "$ref": "#/components/schemas/fyp_ranking"
          }
        },
        "example": {
          "author": "cryptoandcoffee",
          "permlink": "will-we-see-the-hydration-break-next-season",
          "category": "hive-115814",
          "title": "Will We See The Hydration / Advertising Break In The Premier League Next Season?",
          "body": "<center><img src=\"https://images.hive.blog/p/cover.png\"/></center>\n\nMarkdown body of the post...",
          "json_metadata": {
            "app": "leothreads/0.3",
            "format": "markdown",
            "tags": [
              "sports",
              "football",
              "premierleague",
              "advertising"
            ],
            "image": [
              "https://images.hive.blog/p/cover.png"
            ]
          },
          "created": "2026-06-19T16:33:57",
          "updated": "2026-06-19T16:33:57",
          "depth": 0,
          "children": 4,
          "community": "hive-115814",
          "community_title": "Sportsblock",
          "url": "/hive-115814/@cryptoandcoffee/will-we-see-the-hydration-break-next-season",
          "author_reputation": 84.5,
          "author_role": "guest",
          "author_title": "",
          "payout": 3.655,
          "pending_payout_value": "3.655 HBD",
          "author_payout_value": "0.000 HBD",
          "curator_payout_value": "0.000 HBD",
          "max_accepted_payout": "1000000.000 HBD",
          "percent_hbd": 10000,
          "is_paidout": false,
          "payout_at": "2026-06-26T16:33:57",
          "net_rshares": 50938299772643,
          "beneficiaries": [
            {
              "account": "leo.voter",
              "weight": 500
            }
          ],
          "blacklists": [],
          "reblogs": 0,
          "replies": [],
          "active_votes": [
            {
              "voter": "team",
              "rshares": 30982500342
            },
            {
              "voter": "roelandp",
              "rshares": 15794227518
            }
          ],
          "stats": {
            "hide": false,
            "gray": false,
            "total_votes": 127,
            "flag_weight": 0.0
          },
          "fyp": {
            "source": "personalized",
            "rank": 1,
            "final_score": 0.9178,
            "score_relevance": 0.62,
            "score_recency": 0.9926,
            "score_engagement": 0.9805,
            "score_credibility": 0.5679,
            "community_boost_applied": true,
            "boost_source": "subscribed"
          }
        }
      }
    }
  },
  "paths": {
    "/version": {
      "get": {
        "tags": [
          "Other"
        ],
        "summary": "Application version",
        "description": "Returns the git hash of the currently deployed version.",
        "operationId": "haf_fyp_endpoints.get_version",
        "responses": {
          "200": {
            "description": "Git hash string",
            "content": {
              "application/json": {
                "schema": {
                  "type": "string"
                }
              }
            }
          }
        }
      }
    },
    "/health": {
      "get": {
        "tags": [
          "Other"
        ],
        "summary": "Health check",
        "description": "Returns service health and last processed block number.",
        "operationId": "haf_fyp_endpoints.get_health",
        "responses": {
          "200": {
            "description": "Health status",
            "content": {
              "application/json": {
                "schema": {
                  "x-sql-datatype": "TABLE(status text, last_processed_block int, version text)",
                  "type": "array",
                  "items": {
                    "type": "object",
                    "properties": {
                      "status": {
                        "type": "string",
                        "example": "ok"
                      },
                      "last_processed_block": {
                        "type": "integer"
                      },
                      "version": {
                        "type": "string"
                      }
                    }
                  }
                }
              }
            }
          }
        }
      }
    },
    "/sync-status": {
      "get": {
        "tags": [
          "Other"
        ],
        "summary": "Sync status",
        "description": "Last block consumed by the haf_fyp HAF context, and its timestamp. The uniform HAF-app freshness endpoint: same two keys and the same timestamp format as haf_stats' /sync-status. NOTE this reports REACHABILITY and the context head, not liveness -- a stalled app whose context sits at block 0 answers 200 with a null timestamp, which is a state observed in the wild (tests/ci_invariants.sql). Pair it with a block-freshness agent-check, as haf_api_node does for its own apps.",
        "operationId": "haf_fyp_endpoints.get_sync_status",
        "responses": {
          "200": {
            "description": "last_block_time is null when that block has no hafd.blocks row - i.e. nothing has been processed yet (there is no block 0), or the node is pruned and the context is behind the prune tail.",
            "content": {
              "application/json": {
                "schema": {
                  "x-sql-datatype": "JSON",
                  "type": "object",
                  "required": [
                    "last_block_num",
                    "last_block_time"
                  ],
                  "properties": {
                    "last_block_num": {
                      "type": "integer",
                      "description": "last block consumed by the haf_fyp HAF context"
                    },
                    "last_block_time": {
                      "type": [
                        "string",
                        "null"
                      ],
                      "format": "date-time",
                      "description": "UTC timestamp of that block; null if it has no hafd.blocks row"
                    }
                  }
                },
                "example": {
                  "last_block_num": 109603308,
                  "last_block_time": "2026-09-03T22:46:54"
                }
              }
            }
          },
          "500": {
            "description": "Unavailable, and the two cases are distinguished by message rather than by status. Either the HAF instance is still in massive sync (hafd.blocks' primary key is dropped for the duration, so the call fails fast instead of seq-scanning the largest table in the database), or the HAF context is not registered on this node. Both are raised as SQLSTATE 55000 -- the code every other HAF app uses for the massive-sync guard -- which PostgREST maps to 500. 503 would be the better status for a temporary condition, but it is not what the ecosystem does, and one app diverging is worse than the imperfection; changing it belongs everywhere at once.",
            "content": {
              "application/json": {
                "schema": {
                  "type": "object",
                  "properties": {
                    "code": {
                      "type": "string",
                      "description": "PostgreSQL SQLSTATE, '55000' for both cases"
                    },
                    "message": {
                      "type": "string"
                    },
                    "details": {
                      "type": [
                        "string",
                        "null"
                      ]
                    },
                    "hint": {
                      "type": [
                        "string",
                        "null"
                      ]
                    }
                  }
                },
                "example": {
                  "code": "55000",
                  "message": "HAF instance is not ready (massive sync in progress)",
                  "details": null,
                  "hint": null
                }
              }
            }
          }
        }
      }
    },
    "/v1/fyp/algorithm": {
      "get": {
        "tags": [
          "Algorithm"
        ],
        "summary": "Active algorithm",
        "description": "Returns the currently active scoring algorithm version and its weights.",
        "operationId": "haf_fyp_endpoints.get_algorithm",
        "responses": {
          "200": {
            "description": "Active algorithm version",
            "content": {
              "application/json": {
                "schema": {
                  "x-sql-datatype": "TABLE(version text, description text, weights jsonb, deployed_at timestamp)",
                  "type": "array",
                  "items": {
                    "type": "object",
                    "properties": {
                      "version": {
                        "type": "string",
                        "example": "1.0.0"
                      },
                      "description": {
                        "type": "string"
                      },
                      "weights": {
                        "type": "object",
                        "additionalProperties": true,
                        "example": {
                          "relevance": 0.4,
                          "recency": 0.3,
                          "engagement": 0.2,
                          "credibility": 0.1,
                          "community_boost": 0.2
                        }
                      },
                      "deployed_at": {
                        "type": "string",
                        "format": "date-time"
                      }
                    }
                  }
                }
              }
            }
          }
        }
      }
    },
    "/v1/fyp/algorithms": {
      "get": {
        "tags": [
          "Algorithm"
        ],
        "summary": "All algorithm versions",
        "description": "Lists all algorithm versions in the registry with their weights and active status.",
        "operationId": "haf_fyp_endpoints.list_algorithm_versions",
        "responses": {
          "200": {
            "description": "List of all algorithm versions",
            "content": {
              "application/json": {
                "schema": {
                  "x-sql-datatype": "TABLE(version text, description text, weights jsonb, deployed_at timestamp, is_active boolean)",
                  "type": "array",
                  "items": {
                    "type": "object",
                    "properties": {
                      "version": {
                        "type": "string"
                      },
                      "description": {
                        "type": "string"
                      },
                      "weights": {
                        "type": "object",
                        "additionalProperties": true
                      },
                      "deployed_at": {
                        "type": "string",
                        "format": "date-time"
                      },
                      "is_active": {
                        "type": "boolean"
                      }
                    }
                  }
                }
              }
            }
          }
        }
      }
    },
    "/v1/fyp/feed/{username}": {
      "get": {
        "tags": [
          "Feed"
        ],
        "summary": "Personalized feed",
        "description": "Returns a ranked list of posts for the given user, ordered by final score. Each post is returned in the same shape as Hivemind's bridge.get_ranked_posts (the structure front ends already render for trending / hot / new), plus a nested \"fyp\" object carrying this app's ranking data, so the feed is a drop-in alternative content source. Falls back to the global feed until the ranker has cached a feed for the user.",
        "operationId": "haf_fyp_endpoints.get_personalized_feed",
        "parameters": [
          {
            "in": "path",
            "name": "username",
            "required": true,
            "schema": {
              "type": "string"
            },
            "description": "Hive account name"
          },
          {
            "in": "query",
            "name": "page",
            "required": false,
            "schema": {
              "type": "integer",
              "default": 1
            },
            "description": "1-based page number"
          },
          {
            "in": "query",
            "name": "page-size",
            "required": false,
            "schema": {
              "type": "integer",
              "default": 20
            },
            "description": "Posts per page (max 20)"
          },
          {
            "in": "query",
            "name": "truncate_body",
            "required": false,
            "schema": {
              "type": "integer",
              "default": 0
            },
            "description": "Truncate each post body to this many characters (0 = full body)"
          }
        ],
        "responses": {
          "200": {
            "description": "Ranked post list (empty array before first ranker cycle)",
            "content": {
              "application/json": {
                "schema": {
                  "x-sql-datatype": "jsonb",
                  "type": "array",
                  "items": {
                    "$ref": "#/components/schemas/feed_entry"
                  }
                }
              }
            }
          }
        }
      }
    },
    "/v1/fyp/global": {
      "get": {
        "tags": [
          "Feed"
        ],
        "summary": "Global feed",
        "description": "Returns the global unauthenticated feed, optionally filtered by community. Each post is returned in the same shape as Hivemind's bridge.get_ranked_posts, plus a nested \"fyp\" object with this app's ranking data, so the feed is a drop-in alternative content source for front ends.",
        "operationId": "haf_fyp_endpoints.get_global_feed",
        "parameters": [
          {
            "in": "query",
            "name": "page",
            "required": false,
            "schema": {
              "type": "integer",
              "default": 1
            },
            "description": "1-based page number"
          },
          {
            "in": "query",
            "name": "page-size",
            "required": false,
            "schema": {
              "type": "integer",
              "default": 20
            },
            "description": "Posts per page (max 20)"
          },
          {
            "in": "query",
            "name": "community_id",
            "required": false,
            "schema": {
              "type": [
                "integer",
                "null"
              ],
              "default": null
            },
            "description": "Filter results to a specific community (optional)"
          },
          {
            "in": "query",
            "name": "truncate_body",
            "required": false,
            "schema": {
              "type": "integer",
              "default": 0
            },
            "description": "Truncate each post body to this many characters (0 = full body)"
          }
        ],
        "responses": {
          "200": {
            "description": "Global ranked post list",
            "content": {
              "application/json": {
                "schema": {
                  "x-sql-datatype": "jsonb",
                  "type": "array",
                  "items": {
                    "$ref": "#/components/schemas/feed_entry"
                  }
                }
              }
            }
          }
        }
      }
    },
    "/v1/fyp/profile/{username}": {
      "get": {
        "tags": [
          "User"
        ],
        "summary": "User profile",
        "description": "Returns a user's computed interest profile - whether a vector has been built and which communities are tracked.",
        "operationId": "haf_fyp_endpoints.get_user_profile",
        "parameters": [
          {
            "in": "path",
            "name": "username",
            "required": true,
            "schema": {
              "type": "string"
            },
            "description": "Hive account name"
          }
        ],
        "responses": {
          "200": {
            "description": "User profile data (always one row; has_feed/has_vector are false if the user has not been ranked yet)",
            "content": {
              "application/json": {
                "schema": {
                  "x-sql-datatype": "TABLE(has_feed boolean, has_vector boolean, posts_sampled int, top_communities int[], updated_at timestamp)",
                  "type": "array",
                  "items": {
                    "type": "object",
                    "properties": {
                      "has_feed": {
                        "type": "boolean",
                        "description": "True if the user has a precomputed personalized feed (vs the global fallback)"
                      },
                      "has_vector": {
                        "type": "boolean"
                      },
                      "posts_sampled": {
                        "type": "integer"
                      },
                      "top_communities": {
                        "type": "array",
                        "items": {
                          "type": "integer"
                        }
                      },
                      "updated_at": {
                        "type": "string",
                        "format": "date-time"
                      }
                    }
                  }
                }
              }
            }
          }
        }
      }
    },
    "/v1/fyp/explain/{username}/{post_id}": {
      "get": {
        "tags": [
          "Feed"
        ],
        "summary": "Score explanation",
        "description": "Returns a per-signal score breakdown for a specific post in a user's feed cache.",
        "operationId": "haf_fyp_endpoints.get_score_explanation",
        "parameters": [
          {
            "in": "path",
            "name": "username",
            "required": true,
            "schema": {
              "type": "string"
            },
            "description": "Hive account name"
          },
          {
            "in": "path",
            "name": "post_id",
            "required": true,
            "schema": {
              "x-sql-datatype": "BIGINT",
              "type": "integer"
            },
            "description": "Internal post ID"
          }
        ],
        "responses": {
          "200": {
            "description": "Score breakdown (empty if post not in user's feed cache)",
            "content": {
              "application/json": {
                "schema": {
                  "x-sql-datatype": "TABLE(post_id bigint, author varchar(16), permlink varchar(256), title text, final_score float, score_relevance float, score_recency float, score_engagement float, score_credibility float, community_boost_applied boolean, boost_source varchar(16), rank int, algorithm_version varchar(16), algorithm_weights jsonb, computed_at timestamp)",
                  "type": "array",
                  "items": {
                    "type": "object",
                    "properties": {
                      "post_id": {
                        "type": "integer"
                      },
                      "author": {
                        "type": "string"
                      },
                      "permlink": {
                        "type": "string"
                      },
                      "title": {
                        "type": "string"
                      },
                      "final_score": {
                        "type": "number"
                      },
                      "score_relevance": {
                        "type": "number"
                      },
                      "score_recency": {
                        "type": "number"
                      },
                      "score_engagement": {
                        "type": "number"
                      },
                      "score_credibility": {
                        "type": "number"
                      },
                      "community_boost_applied": {
                        "type": "boolean"
                      },
                      "boost_source": {
                        "type": "string",
                        "nullable": true,
                        "description": "Which tier applied the community boost - 'subscribed', 'engaged', or null"
                      },
                      "rank": {
                        "type": "integer"
                      },
                      "algorithm_version": {
                        "type": "string"
                      },
                      "algorithm_weights": {
                        "type": "object",
                        "additionalProperties": true
                      },
                      "computed_at": {
                        "type": "string",
                        "format": "date-time"
                      }
                    }
                  }
                }
              }
            }
          }
        }
      }
    },
    "/v1/fyp/feed-metrics": {
      "get": {
        "tags": [
          "Feed"
        ],
        "summary": "Feed quality metrics",
        "description": "Aggregated feed-quality metrics from the telemetry rollup (feed_metrics_daily, #12), summed over a date range and grouped by algorithm_version / source / rank. Enables engagement-rate@rank, open rate, and average dwell/watch \u2014 and A/B comparison across algorithm_version. NON-AUTHORITATIVE: derived from the unauthenticated /v1/fyp/events log (username is client-claimed), so treat figures as directional and corroborate against on-chain engagement. Global-feed responses now carry the algorithm_version that produced them (#23), so clients that echo it back yield attributed global rows; the server never derives this column, so anything a client omits still lands in the '' bucket (feed_metrics_daily.algorithm_version is NOT NULL DEFAULT ''). Rows recorded before #23 sit in '' for the same reason and are not backfilled. The global feed is identical for every viewer, so global-source attribution supports longitudinal before/after comparison rather than concurrent A/B arms.",
        "operationId": "haf_fyp_endpoints.get_feed_metrics",
        "parameters": [
          {
            "in": "query",
            "name": "from_date",
            "required": false,
            "schema": {
              "x-sql-datatype": "DATE",
              "type": [
                "string",
                "null"
              ],
              "format": "date",
              "default": null
            },
            "description": "Inclusive start date (YYYY-MM-DD). Default 30 days ago."
          },
          {
            "in": "query",
            "name": "to_date",
            "required": false,
            "schema": {
              "x-sql-datatype": "DATE",
              "type": [
                "string",
                "null"
              ],
              "format": "date",
              "default": null
            },
            "description": "Inclusive end date (YYYY-MM-DD). Default today."
          },
          {
            "in": "query",
            "name": "algorithm_version",
            "required": false,
            "schema": {
              "type": [
                "string",
                "null"
              ],
              "default": null
            },
            "description": "Filter to one algorithm version (optional)"
          },
          {
            "in": "query",
            "name": "source",
            "required": false,
            "schema": {
              "type": [
                "string",
                "null"
              ],
              "default": null
            },
            "description": "Filter to 'personalized' or 'global' (optional)"
          }
        ],
        "responses": {
          "200": {
            "description": "Aggregated metric rows",
            "content": {
              "application/json": {
                "schema": {
                  "x-sql-datatype": "jsonb",
                  "type": "array",
                  "items": {
                    "type": "object",
                    "properties": {
                      "algorithm_version": {
                        "type": "string"
                      },
                      "source": {
                        "type": "string"
                      },
                      "rank": {
                        "type": "integer"
                      },
                      "impressions": {
                        "type": "integer"
                      },
                      "opens": {
                        "type": "integer"
                      },
                      "actions": {
                        "type": "integer"
                      },
                      "videos": {
                        "type": "integer"
                      },
                      "engagement_rate": {
                        "type": "number",
                        "nullable": true,
                        "description": "actions (vote+comment+reblog+follow) / impressions"
                      },
                      "open_rate": {
                        "type": "number",
                        "nullable": true,
                        "description": "opens / impressions (closest to a click-through)"
                      },
                      "avg_dwell_ms": {
                        "type": "number",
                        "nullable": true
                      },
                      "avg_watch_ms": {
                        "type": "number",
                        "nullable": true
                      }
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
$$;
-- openapi-generated-code-end
BEGIN
    RETURN openapi;
END;
$func$;

-- ---------------------------------------------------------------------------
-- Health / version
-- ---------------------------------------------------------------------------

/** openapi:paths
/version:
  get:
    tags:
      - Other
    summary: Application version
    description: Returns the git hash of the currently deployed version.
    operationId: haf_fyp_endpoints.get_version
    responses:
      '200':
        description: Git hash string
        content:
          application/json:
            schema:
              type: string
 */
-- openapi-generated-code-begin
DROP FUNCTION IF EXISTS haf_fyp_endpoints.get_version;
CREATE OR REPLACE FUNCTION haf_fyp_endpoints.get_version()
RETURNS TEXT
-- openapi-generated-code-end
LANGUAGE sql STABLE
AS $$
    SELECT git_hash FROM haf_fyp_app.app_version LIMIT 1;
$$;

/** openapi:paths
/health:
  get:
    tags:
      - Other
    summary: Health check
    description: Returns service health and last processed block number.
    operationId: haf_fyp_endpoints.get_health
    responses:
      '200':
        description: Health status
        content:
          application/json:
            schema:
              x-sql-datatype: "TABLE(status text, last_processed_block int, version text)"
              type: array
              items:
                type: object
                properties:
                  status:
                    type: string
                    example: ok
                  last_processed_block:
                    type: integer
                  version:
                    type: string
 */
-- openapi-generated-code-begin
DROP FUNCTION IF EXISTS haf_fyp_endpoints.get_health;
CREATE OR REPLACE FUNCTION haf_fyp_endpoints.get_health()
RETURNS TABLE(status text, last_processed_block int, version text)
-- openapi-generated-code-end
LANGUAGE sql STABLE
AS $$
    SELECT
        'ok'::text AS status,
        s.last_processed_block,
        (SELECT git_hash FROM haf_fyp_app.app_version LIMIT 1) AS version
    FROM haf_fyp_app.app_status s
    LIMIT 1;
$$;

/** openapi:paths
/sync-status:
  get:
    tags:
      - Other
    summary: Sync status
    description: >-
      Last block consumed by the haf_fyp HAF context, and its timestamp. The
      uniform HAF-app freshness endpoint: same two keys and the same timestamp
      format as haf_stats' /sync-status. NOTE this reports REACHABILITY and the
      context head, not liveness -- a stalled app whose context sits at block 0
      answers 200 with a null timestamp, which is a state observed in the wild
      (tests/ci_invariants.sql). Pair it with a block-freshness agent-check, as
      haf_api_node does for its own apps.
    operationId: haf_fyp_endpoints.get_sync_status
    responses:
      '200':
        description: >-
          last_block_time is null when that block has no hafd.blocks row - i.e.
          nothing has been processed yet (there is no block 0), or the node is
          pruned and the context is behind the prune tail.
        content:
          application/json:
            schema:
              x-sql-datatype: JSON
              type: object
              required: [last_block_num, last_block_time]
              properties:
                last_block_num:
                  type: integer
                  description: last block consumed by the haf_fyp HAF context
                last_block_time:
                  # ["string", "null"], not `nullable: true`: this document declares
                  # openapi 3.1.0, where `nullable` was removed and is inert.
                  type:
                    - string
                    - "null"
                  format: date-time
                  description: >-
                    UTC timestamp of that block; null if it has no hafd.blocks row
            example:
              last_block_num: 109603308
              last_block_time: '2026-09-03T22:46:54'
      '500':
        description: >-
          Unavailable, and the two cases are distinguished by message rather than by
          status. Either the HAF instance is still in massive sync (hafd.blocks'
          primary key is dropped for the duration, so the call fails fast instead of
          seq-scanning the largest table in the database), or the HAF context is not
          registered on this node. Both are raised as SQLSTATE 55000 -- the code
          every other HAF app uses for the massive-sync guard -- which PostgREST
          maps to 500. 503 would be the better status for a temporary condition, but
          it is not what the ecosystem does, and one app diverging is worse than the
          imperfection; changing it belongs everywhere at once.
        content:
          application/json:
            schema:
              type: object
              properties:
                code:
                  type: string
                  description: PostgreSQL SQLSTATE, '55000' for both cases
                message:
                  type: string
                details:
                  type:
                    - string
                    - "null"
                hint:
                  type:
                    - string
                    - "null"
            example:
              code: '55000'
              message: HAF instance is not ready (massive sync in progress)
              details: null
              hint: null
 */
-- openapi-generated-code-begin
DROP FUNCTION IF EXISTS haf_fyp_endpoints.get_sync_status;
CREATE OR REPLACE FUNCTION haf_fyp_endpoints.get_sync_status()
RETURNS JSON
-- openapi-generated-code-end
LANGUAGE plpgsql STABLE
AS $$
DECLARE
    _s RECORD;
BEGIN
    -- Never cache: a freshness probe served from cache reports the cache's age,
    -- not the app's. HAProxy's per-app backend check reads this endpoint.
    PERFORM set_config('response.headers', '[{"Cache-Control": "public, max-age=0"}]', true);

    -- haf_fyp_app.sync_status() is SECURITY DEFINER (db/sync_status.sql): hafd.contexts
    -- has RLS whose only application SELECT policy is granted TO hive_applications_group,
    -- and haf_fyp_user deliberately stays out of that group. The definer owns the
    -- massive-sync guard and the context name; this function owns only the wire format.
    --
    -- The definer's 55000 ("instance not ready") is passed straight through, because
    -- 55000 is the ecosystem standard for this condition: hafah, haf_block_explorer,
    -- reputation_tracker, balance_tracker, nft_tracker and hivesense all raise exactly
    -- that, and so does haf_fyp's own definer. haf_stats is the only app using 53000.
    --
    -- An earlier revision here re-raised it as 53000 so PostgREST would answer 503
    -- rather than 500 (it maps 53xxx -> 503 and 55xxx -> 500). 503 IS the better
    -- status for a temporary, self-healing condition -- but making one app diverge is
    -- worse than the imperfection: a consumer written against HAF apps would then get
    -- 500 from six of them and 503 from this one for the identical state, which
    -- defeats the point of a uniform endpoint. It also changes nothing for the health
    -- check, since HAProxy's `expect status 200` rejects both. If 503 is right it
    -- should change everywhere at once -- filed separately.
    SELECT * INTO _s FROM haf_fyp_app.sync_status();

    -- Zero rows means the HAF context has not been registered yet, or it was removed.
    -- Answering 200 with a null body would merge those with "healthy" and hide a
    -- misconfiguration behind a healthy-looking response.
    --
    -- It does NOT cover haf_fyp_owner losing hive_applications_group. That was the
    -- original claim here and it is wrong: the definer's FIRST statement calls
    -- hive.is_instance_ready(), whose EXECUTE comes only through that group, so the
    -- loss raises 42501 before any RLS-filtered row is read -- and PostgREST maps
    -- 42501 on an anonymous request to 401, not 503. A probe expecting 200-or-503
    -- misreads that as an auth failure. Detecting it belongs in the installer's
    -- privilege checks, not here.
    -- No other app has this branch -- hafah and friends build the JSON unconditionally
    -- -- so it is an addition rather than a divergence, and it uses the SAME error
    -- class as the guard above so haf_fyp emits exactly one, like every other app.
    IF NOT FOUND THEN
        RAISE EXCEPTION 'HAF context ''haf_fyp'' is not registered on this node, or it was removed'
            USING ERRCODE = '55000';
    END IF;

    -- Two keys only. The CONTRACT is the key names and the timestamp format --
    -- literal 'T', 19 characters, no fractional seconds, no timezone suffix --
    -- because consumers parse it with a fixed format, so drift there is a silent
    -- breaking change.
    --
    -- Byte-identical to haf_stats' /sync-status, which builds it the same way.
    -- NOT byte-identical to this app's own FastAPI twin: json_build_object (json,
    -- not jsonb) emits spaces around colons, `{"k" : v}`, where FastAPI emits
    -- `{"k":v}`. Semantically identical, and no consumer should be byte-comparing
    -- JSON -- but do not restate the stronger claim, it was measured false.
    -- hafbe, hafah and balance_tracker serve no /sync-status at all.
    RETURN json_build_object(
        'last_block_num', _s.last_block_num,
        'last_block_time', to_char(_s.last_block_time, 'YYYY-MM-DD"T"HH24:MI:SS')
    );
END
$$;

-- ---------------------------------------------------------------------------
-- Algorithm version info
-- ---------------------------------------------------------------------------

/** openapi:paths
/v1/fyp/algorithm:
  get:
    tags:
      - Algorithm
    summary: Active algorithm
    description: Returns the currently active scoring algorithm version and its weights.
    operationId: haf_fyp_endpoints.get_algorithm
    responses:
      '200':
        description: Active algorithm version
        content:
          application/json:
            schema:
              x-sql-datatype: "TABLE(version text, description text, weights jsonb, deployed_at timestamp)"
              type: array
              items:
                type: object
                properties:
                  version:
                    type: string
                    example: "1.0.0"
                  description:
                    type: string
                  weights:
                    type: object
                    additionalProperties: true
                    example:
                      relevance: 0.40
                      recency: 0.30
                      engagement: 0.20
                      credibility: 0.10
                      community_boost: 0.20
                  deployed_at:
                    type: string
                    format: date-time
 */
-- openapi-generated-code-begin
DROP FUNCTION IF EXISTS haf_fyp_endpoints.get_algorithm;
CREATE OR REPLACE FUNCTION haf_fyp_endpoints.get_algorithm()
RETURNS TABLE(version text, description text, weights jsonb, deployed_at timestamp)
-- openapi-generated-code-end
LANGUAGE sql STABLE
AS $$
    SELECT version, description, weights, deployed_at
    FROM haf_fyp_app.algorithm_versions
    WHERE is_active = TRUE
    LIMIT 1;
$$;

/** openapi:paths
/v1/fyp/algorithms:
  get:
    tags:
      - Algorithm
    summary: All algorithm versions
    description: Lists all algorithm versions in the registry with their weights and active status.
    operationId: haf_fyp_endpoints.list_algorithm_versions
    responses:
      '200':
        description: List of all algorithm versions
        content:
          application/json:
            schema:
              x-sql-datatype: "TABLE(version text, description text, weights jsonb, deployed_at timestamp, is_active boolean)"
              type: array
              items:
                type: object
                properties:
                  version:
                    type: string
                  description:
                    type: string
                  weights:
                    type: object
                    additionalProperties: true
                  deployed_at:
                    type: string
                    format: date-time
                  is_active:
                    type: boolean
 */
-- openapi-generated-code-begin
DROP FUNCTION IF EXISTS haf_fyp_endpoints.list_algorithm_versions;
CREATE OR REPLACE FUNCTION haf_fyp_endpoints.list_algorithm_versions()
RETURNS TABLE(version text, description text, weights jsonb, deployed_at timestamp, is_active boolean)
-- openapi-generated-code-end
LANGUAGE sql STABLE
AS $$
    SELECT version, description, weights, deployed_at, is_active
    FROM haf_fyp_app.algorithm_versions
    ORDER BY deployed_at DESC;
$$;

-- ---------------------------------------------------------------------------
-- Bridge post enrichment
--
-- Builds the exact post object that Hivemind's bridge.get_ranked_posts returns
-- (the structure every Hive front end already renders for trending / hot / new),
-- so an FYP feed can be dropped in as an alternative content source with no
-- rendering changes. It reuses Hivemind's own get_full_post_view_by_id +
-- create_bridge_post_object rather than duplicating the payout / reputation /
-- vote math, which keeps the output byte-identical to bridge as Hivemind evolves.
-- Returns NULL when the post no longer exists in Hivemind (e.g. it was deleted
-- after the ranker cached it) so the caller can filter it out of the feed.
--
-- _post_id here is the Hivemind hive_posts.id (resolved by the feed endpoints
-- from the cached (author, permlink)), NOT the FYP post_candidates.post_id.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION haf_fyp_app.get_bridge_post(
    _post_id int,
    _observer_id int,
    _truncate_body int
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    _result jsonb;
BEGIN
    -- Clamp: create_bridge_post_object treats 0 as "no truncation"; a negative
    -- value would make left(body, n) keep "all but the last n chars". 0 = full.
    _truncate_body := GREATEST(_truncate_body, 0);
    SELECT hivemind_postgrest_utilities.create_bridge_post_object(
        _observer_id, r, _truncate_body, NULL, r.is_pinned, TRUE
    )
    INTO _result
    FROM (
        SELECT
            hp.id, hp.author, hp.parent_author, hp.author_rep, hp.root_title,
            hp.beneficiaries, hp.max_accepted_payout, hp.percent_hbd, hp.url,
            hp.permlink, hp.parent_permlink_or_category, hp.title, hp.body,
            hp.category, hp.depth, hp.payout, hp.pending_payout, hp.payout_at,
            hp.is_paidout, hp.children, hp.votes, hp.created_at, hp.updated_at,
            hp.rshares, hp.abs_rshares, hp.json, hp.is_hidden, hp.is_grayed,
            hp.total_votes, hp.sc_trend, hp.role_title, hp.community_title,
            hp.role_id, hp.is_pinned, hp.curator_payout_value, hp.is_muted,
            hp.source AS blacklists, hp.muted_reasons
        FROM hivemind_app.get_full_post_view_by_id(_post_id, _observer_id) AS hp
    ) AS r;
    RETURN _result;
END;
$$;

GRANT EXECUTE ON FUNCTION haf_fyp_app.get_bridge_post(int, int, int) TO haf_fyp_user;

-- ---------------------------------------------------------------------------
-- Personalized feed
-- ---------------------------------------------------------------------------

/** openapi:paths
/v1/fyp/feed/{username}:
  get:
    tags:
      - Feed
    summary: Personalized feed
    description: >-
      Returns a ranked list of posts for the given user, ordered by final score.
      Each post is returned in the same shape as Hivemind's
      bridge.get_ranked_posts (the structure front ends already render for
      trending / hot / new), plus a nested "fyp" object carrying this app's
      ranking data, so the feed is a drop-in alternative content source.
      Falls back to the global feed until the ranker has cached a feed for the user.
    operationId: haf_fyp_endpoints.get_personalized_feed
    parameters:
      - in: path
        name: username
        required: true
        schema:
          type: string
        description: Hive account name
      - in: query
        name: page
        required: false
        schema:
          type: integer
          default: 1
        description: 1-based page number
      - in: query
        name: page-size
        required: false
        schema:
          type: integer
          default: 20
        description: Posts per page (max 20)
      - in: query
        name: truncate_body
        required: false
        schema:
          type: integer
          default: 0
        description: Truncate each post body to this many characters (0 = full body)
    responses:
      '200':
        description: Ranked post list (empty array before first ranker cycle)
        content:
          application/json:
            schema:
              x-sql-datatype: "jsonb"
              type: array
              items:
                $ref: '#/components/schemas/feed_entry'
 */
-- openapi-generated-code-begin
DROP FUNCTION IF EXISTS haf_fyp_endpoints.get_personalized_feed;
CREATE OR REPLACE FUNCTION haf_fyp_endpoints.get_personalized_feed(
    "username" TEXT,
    "page" INT = 1,
    "page-size" INT = 20,
    "truncate_body" INT = 0
)
RETURNS jsonb
-- openapi-generated-code-end
LANGUAGE plpgsql STABLE
AS $$
DECLARE
    _observer_id int;
    _result jsonb;
    -- Pagination uses page / page-size (not limit / offset, which PostgREST
    -- reserves for result-set slicing and would not forward as arguments).
    -- Enrichment is the costly step, so the window is applied in-query and
    -- page-size is capped to bound per-request work.
    _limit int := LEAST(GREATEST(get_personalized_feed."page-size", 1), 20);
    _offset int := (GREATEST(get_personalized_feed."page", 1) - 1) * _limit;
BEGIN
    -- Personalized feed. Each cached (author, permlink) is resolved to its
    -- Hivemind post id and enriched into the bridge post object, then tagged
    -- with the FYP ranking data under "fyp". The Hivemind joins are reached only
    -- when the user actually has a cached feed, so an empty feed never touches
    -- Hivemind (keeps the endpoint usable before the ranker runs / in CI).
    IF EXISTS (
        SELECT 1 FROM haf_fyp_app.feed_cache fc
        WHERE fc.username = get_personalized_feed.username
    ) THEN
        -- Observer context makes active_votes / muted state account-aware,
        -- exactly as a logged-in trending feed would be.
        _observer_id := COALESCE(
            (SELECT ha.id FROM hivemind_app.hive_accounts ha
             WHERE ha.name = get_personalized_feed.username), 0
        );
        SELECT COALESCE(
            jsonb_agg(s.post ORDER BY s.rank) FILTER (WHERE s.post IS NOT NULL),
            '[]'::jsonb
        )
        INTO _result
        FROM (
            -- Enrich only the page: get_bridge_post runs at this level, over the
            -- already-windowed "page" rows, so the Hivemind builders are called
            -- at most page-size times regardless of the join plan (the inner
            -- LIMIT is a cardinality barrier the projection cannot cross).
            SELECT
                page.rank,
                -- Serve the pre-rendered bridge JSON from post_bridge_cache when
                -- present (re-applying truncate_body to the cached full body),
                -- else render live. The cache is observer-agnostic (observer_id 0);
                -- the live fallback keeps observer context on a miss.
                COALESCE(
                    CASE
                        WHEN page.cached IS NOT NULL AND get_personalized_feed.truncate_body > 0
                        THEN jsonb_set(
                                 page.cached, '{body}',
                                 to_jsonb(left(page.cached ->> 'body', get_personalized_feed.truncate_body))
                             )
                        ELSE page.cached
                    END,
                    haf_fyp_app.get_bridge_post(page.hive_post_id, _observer_id, get_personalized_feed.truncate_body)
                )
                || jsonb_build_object('fyp', page.fyp) AS post
            FROM (
                SELECT
                    pc.hive_post_id AS hive_post_id,
                    pbc.bridge AS cached,
                    fc.rank AS rank,
                    jsonb_build_object(
                        'source', 'personalized',
                        'rank', fc.rank,
                        'final_score', fc.final_score,
                        'score_relevance', fc.score_relevance,
                        'score_recency', fc.score_recency,
                        'score_engagement', fc.score_engagement,
                        'score_credibility', fc.score_credibility,
                        'community_boost_applied', fc.community_boost_applied,
                        'boost_source', fc.boost_source,
                        -- Exposed for client telemetry (#12): post_id is the
                        -- joinable key (matches feed_events.post_id) and
                        -- algorithm_version lets impressions be sliced for A/B.
                        'post_id', fc.post_id::text,
                        'algorithm_version', fc.algorithm_version
                    ) AS fyp
                -- hive_post_id is precomputed by resolve_hivemind_post_ids (see
                -- db/process_posts.sql), so the feed no longer joins
                -- hive_accounts / hive_permlink_data / hive_posts per request; it
                -- is a single PK join to post_candidates. Posts not yet resolved
                -- (hive_post_id IS NULL) are skipped until a later processor batch
                -- resolves them.
                FROM haf_fyp_app.feed_cache fc
                JOIN haf_fyp_app.post_candidates pc ON pc.post_id = fc.post_id
                LEFT JOIN haf_fyp_app.post_bridge_cache pbc ON pbc.post_id = fc.post_id
                WHERE fc.username = get_personalized_feed.username
                  AND pc.hive_post_id IS NOT NULL
                ORDER BY fc.rank
                LIMIT _limit
                OFFSET _offset
            ) AS page
        ) AS s;
        RETURN _result;
    END IF;

    -- Cold-start fallback: a user without a cached personalized feed gets the
    -- global feed instead of an empty result, so the "For You" page is never
    -- blank. Personalization kicks in automatically once the ranker caches a feed.
    IF EXISTS (SELECT 1 FROM haf_fyp_app.global_feed_cache) THEN
        _observer_id := COALESCE(
            (SELECT ha.id FROM hivemind_app.hive_accounts ha
             WHERE ha.name = get_personalized_feed.username), 0
        );
        SELECT COALESCE(
            jsonb_agg(s.post ORDER BY s.rank) FILTER (WHERE s.post IS NOT NULL),
            '[]'::jsonb
        )
        INTO _result
        FROM (
            SELECT
                page.rank,
                -- Serve the pre-rendered bridge JSON from post_bridge_cache when
                -- present (re-applying truncate_body to the cached full body),
                -- else render live. The cache is observer-agnostic (observer_id 0);
                -- the live fallback keeps observer context on a miss.
                COALESCE(
                    CASE
                        WHEN page.cached IS NOT NULL AND get_personalized_feed.truncate_body > 0
                        THEN jsonb_set(
                                 page.cached, '{body}',
                                 to_jsonb(left(page.cached ->> 'body', get_personalized_feed.truncate_body))
                             )
                        ELSE page.cached
                    END,
                    haf_fyp_app.get_bridge_post(page.hive_post_id, _observer_id, get_personalized_feed.truncate_body)
                )
                || jsonb_build_object('fyp', page.fyp) AS post
            FROM (
                SELECT
                    pc.hive_post_id AS hive_post_id,
                    pbc.bridge AS cached,
                    gfc.rank AS rank,
                    jsonb_build_object(
                        'source', 'global',
                        'rank', gfc.rank,
                        'final_score', gfc.final_score,
                        'score_relevance', NULL::float,
                        'score_recency', gfc.score_recency,
                        'score_engagement', gfc.score_engagement,
                        'score_credibility', gfc.score_credibility,
                        'community_boost_applied', FALSE,
                        'boost_source', NULL::text,
                        -- #12: joinable key for client telemetry.
                        'post_id', gfc.post_id::text,
                        -- Stamped by the ranker (#23); was hardcoded NULL, which left
                        -- every global-sourced impression unattributable in
                        -- feed_metrics_daily -- including the cold-start fallback this
                        -- endpoint serves to users who have no personalized feed yet.
                        -- The global feed is identical for every viewer, so only one
                        -- version produces it at a time: this buys LONGITUDINAL
                        -- attribution across a version change, not concurrent A/B arms.
                        'algorithm_version', gfc.algorithm_version
                    ) AS fyp
                -- See get_personalized_feed: hive_post_id is precomputed, so no
                -- per-request Hivemind join (single PK join to post_candidates).
                FROM haf_fyp_app.global_feed_cache gfc
                JOIN haf_fyp_app.post_candidates pc ON pc.post_id = gfc.post_id
                LEFT JOIN haf_fyp_app.post_bridge_cache pbc ON pbc.post_id = gfc.post_id
                WHERE pc.hive_post_id IS NOT NULL
                ORDER BY gfc.rank
                LIMIT _limit
                OFFSET _offset
            ) AS page
        ) AS s;
        RETURN _result;
    END IF;

    RETURN '[]'::jsonb;
END;
$$;

-- ---------------------------------------------------------------------------
-- Global (unauthenticated) feed
-- ---------------------------------------------------------------------------

/** openapi:paths
/v1/fyp/global:
  get:
    tags:
      - Feed
    summary: Global feed
    description: >-
      Returns the global unauthenticated feed, optionally filtered by community.
      Each post is returned in the same shape as Hivemind's
      bridge.get_ranked_posts, plus a nested "fyp" object with this app's ranking
      data, so the feed is a drop-in alternative content source for front ends.
    operationId: haf_fyp_endpoints.get_global_feed
    parameters:
      - in: query
        name: page
        required: false
        schema:
          type: integer
          default: 1
        description: 1-based page number
      - in: query
        name: page-size
        required: false
        schema:
          type: integer
          default: 20
        description: Posts per page (max 20)
      - in: query
        name: community_id
        required: false
        schema:
          type: [integer, 'null']
          default: null
        description: Filter results to a specific community (optional)
      - in: query
        name: truncate_body
        required: false
        schema:
          type: integer
          default: 0
        description: Truncate each post body to this many characters (0 = full body)
    responses:
      '200':
        description: Global ranked post list
        content:
          application/json:
            schema:
              x-sql-datatype: "jsonb"
              type: array
              items:
                $ref: '#/components/schemas/feed_entry'
 */
-- openapi-generated-code-begin
DROP FUNCTION IF EXISTS haf_fyp_endpoints.get_global_feed;
CREATE OR REPLACE FUNCTION haf_fyp_endpoints.get_global_feed(
    "page" INT = 1,
    "page-size" INT = 20,
    "community_id" INT = NULL,
    "truncate_body" INT = 0
)
RETURNS jsonb
-- openapi-generated-code-end
LANGUAGE plpgsql STABLE
AS $$
DECLARE
    _result jsonb;
    -- See get_personalized_feed: page / page-size, capped, windowed in-query.
    _limit int := LEAST(GREATEST(get_global_feed."page-size", 1), 20);
    _offset int := (GREATEST(get_global_feed."page", 1) - 1) * _limit;
BEGIN
    -- Global feed is unauthenticated, so observer_id is 0 (no account-specific
    -- vote / mute context). Hivemind is only joined when the cache has rows.
    IF EXISTS (SELECT 1 FROM haf_fyp_app.global_feed_cache) THEN
        SELECT COALESCE(
            jsonb_agg(s.post ORDER BY s.rank) FILTER (WHERE s.post IS NOT NULL),
            '[]'::jsonb
        )
        INTO _result
        FROM (
            -- Enrich only the page (see get_personalized_feed): get_bridge_post
            -- runs over the already-windowed "page" rows, so the Hivemind
            -- builders are called at most page-size times regardless of plan.
            SELECT
                page.rank,
                -- See get_personalized_feed: serve cached bridge if present (the
                -- global feed is observer-agnostic, so the cache is an exact match),
                -- else render live.
                COALESCE(
                    CASE
                        WHEN page.cached IS NOT NULL AND get_global_feed.truncate_body > 0
                        THEN jsonb_set(
                                 page.cached, '{body}',
                                 to_jsonb(left(page.cached ->> 'body', get_global_feed.truncate_body))
                             )
                        ELSE page.cached
                    END,
                    haf_fyp_app.get_bridge_post(page.hive_post_id, 0, get_global_feed.truncate_body)
                )
                || jsonb_build_object('fyp', page.fyp) AS post
            FROM (
                SELECT
                    pc.hive_post_id AS hive_post_id,
                    pbc.bridge AS cached,
                    gfc.rank AS rank,
                    jsonb_build_object(
                        'source', 'global',
                        'rank', gfc.rank,
                        'final_score', gfc.final_score,
                        'score_relevance', NULL::float,
                        'score_recency', gfc.score_recency,
                        'score_engagement', gfc.score_engagement,
                        'score_credibility', gfc.score_credibility,
                        'community_boost_applied', FALSE,
                        'boost_source', NULL::text,
                        -- #12: joinable key for client telemetry.
                        'post_id', gfc.post_id::text,
                        -- Stamped by the ranker (#23); was hardcoded NULL, which left
                        -- every global-sourced impression unattributable in
                        -- feed_metrics_daily.
                        -- The global feed is identical for every viewer, so only one
                        -- version produces it at a time: this buys LONGITUDINAL
                        -- attribution across a version change, not concurrent A/B arms.
                        'algorithm_version', gfc.algorithm_version
                    ) AS fyp
                -- See get_personalized_feed: hive_post_id is precomputed, so no
                -- per-request Hivemind join. The community filter stays on
                -- post_candidates (global_feed_cache is capped at the top ~200 posts,
                -- so this is a tiny PK-joined scan, not a perf concern).
                FROM haf_fyp_app.global_feed_cache gfc
                JOIN haf_fyp_app.post_candidates pc ON pc.post_id = gfc.post_id
                LEFT JOIN haf_fyp_app.post_bridge_cache pbc ON pbc.post_id = gfc.post_id
                WHERE pc.hive_post_id IS NOT NULL
                  AND (
                    get_global_feed.community_id IS NULL
                    OR pc.community_id = get_global_feed.community_id
                  )
                ORDER BY gfc.rank
                LIMIT _limit
                OFFSET _offset
            ) AS page
        ) AS s;
        RETURN _result;
    END IF;

    RETURN '[]'::jsonb;
END;
$$;

-- ---------------------------------------------------------------------------
-- User profile
-- ---------------------------------------------------------------------------

/** openapi:paths
/v1/fyp/profile/{username}:
  get:
    tags:
      - User
    summary: User profile
    description: >-
      Returns a user's computed interest profile - whether a vector has been
      built and which communities are tracked.
    operationId: haf_fyp_endpoints.get_user_profile
    parameters:
      - in: path
        name: username
        required: true
        schema:
          type: string
        description: Hive account name
    responses:
      '200':
        description: User profile data (always one row; has_feed/has_vector are false if the user has not been ranked yet)
        content:
          application/json:
            schema:
              x-sql-datatype: "TABLE(has_feed boolean, has_vector boolean, posts_sampled int, top_communities int[], updated_at timestamp)"
              type: array
              items:
                type: object
                properties:
                  has_feed:
                    type: boolean
                    description: True if the user has a precomputed personalized feed (vs the global fallback)
                  has_vector:
                    type: boolean
                  posts_sampled:
                    type: integer
                  top_communities:
                    type: array
                    items:
                      type: integer
                  updated_at:
                    type: string
                    format: date-time
 */
-- openapi-generated-code-begin
DROP FUNCTION IF EXISTS haf_fyp_endpoints.get_user_profile;
CREATE OR REPLACE FUNCTION haf_fyp_endpoints.get_user_profile(
    "username" TEXT
)
RETURNS TABLE(has_feed boolean, has_vector boolean, posts_sampled int, top_communities int[], updated_at timestamp)
-- openapi-generated-code-end
LANGUAGE sql STABLE
AS $$
    -- Always returns exactly one row, even when the user has no profile yet, so
    -- the endpoint never returns an empty array. has_feed indicates whether the
    -- user has a precomputed personalized feed (vs falling back to the global
    -- feed); has_vector indicates whether a semantic interest vector was built.
    SELECT
        EXISTS (
            SELECT 1 FROM haf_fyp_app.feed_cache fc
            WHERE fc.username = get_user_profile.username
        ) AS has_feed,
        COALESCE(upv.interest_vector IS NOT NULL, FALSE) AS has_vector,
        COALESCE(upv.posts_sampled, 0) AS posts_sampled,
        upv.communities AS top_communities,
        upv.updated_at
    FROM (SELECT get_user_profile.username AS username) base
    LEFT JOIN haf_fyp_app.user_profile_vectors upv USING (username);
$$;

-- ---------------------------------------------------------------------------
-- Score explainer - per-signal breakdown for a post in a user's feed cache
-- ---------------------------------------------------------------------------

/** openapi:paths
/v1/fyp/explain/{username}/{post_id}:
  get:
    tags:
      - Feed
    summary: Score explanation
    description: >-
      Returns a per-signal score breakdown for a specific post in a user's
      feed cache.
    operationId: haf_fyp_endpoints.get_score_explanation
    parameters:
      - in: path
        name: username
        required: true
        schema:
          type: string
        description: Hive account name
      - in: path
        name: post_id
        required: true
        schema:
          x-sql-datatype: BIGINT
          type: integer
        description: Internal post ID
    responses:
      '200':
        description: Score breakdown (empty if post not in user's feed cache)
        content:
          application/json:
            schema:
              x-sql-datatype: "TABLE(post_id bigint, author varchar(16), permlink varchar(256), title text, final_score float, score_relevance float, score_recency float, score_engagement float, score_credibility float, community_boost_applied boolean, boost_source varchar(16), rank int, algorithm_version varchar(16), algorithm_weights jsonb, computed_at timestamp)"
              type: array
              items:
                type: object
                properties:
                  post_id:
                    type: integer
                  author:
                    type: string
                  permlink:
                    type: string
                  title:
                    type: string
                  final_score:
                    type: number
                  score_relevance:
                    type: number
                  score_recency:
                    type: number
                  score_engagement:
                    type: number
                  score_credibility:
                    type: number
                  community_boost_applied:
                    type: boolean
                  boost_source:
                    type: string
                    nullable: true
                    description: Which tier applied the community boost - 'subscribed', 'engaged', or null
                  rank:
                    type: integer
                  algorithm_version:
                    type: string
                  algorithm_weights:
                    type: object
                    additionalProperties: true
                  computed_at:
                    type: string
                    format: date-time
 */
-- openapi-generated-code-begin
DROP FUNCTION IF EXISTS haf_fyp_endpoints.get_score_explanation;
CREATE OR REPLACE FUNCTION haf_fyp_endpoints.get_score_explanation(
    "username" TEXT,
    "post_id" BIGINT
)
RETURNS TABLE(post_id bigint, author varchar(16), permlink varchar(256), title text, final_score float, score_relevance float, score_recency float, score_engagement float, score_credibility float, community_boost_applied boolean, boost_source varchar(16), rank int, algorithm_version varchar(16), algorithm_weights jsonb, computed_at timestamp)
-- openapi-generated-code-end
LANGUAGE sql STABLE
AS $$
    SELECT
        fc.post_id,
        pc.author,
        pc.permlink,
        pc.title,
        fc.final_score,
        fc.score_relevance,
        fc.score_recency,
        fc.score_engagement,
        fc.score_credibility,
        fc.community_boost_applied,
        fc.boost_source,
        fc.rank,
        fc.algorithm_version,
        av.weights AS algorithm_weights,
        fc.computed_at
    FROM haf_fyp_app.feed_cache fc
    JOIN haf_fyp_app.post_candidates pc ON pc.post_id = fc.post_id
    LEFT JOIN haf_fyp_app.algorithm_versions av ON av.version = fc.algorithm_version
    WHERE fc.username = get_score_explanation.username
      AND fc.post_id = get_score_explanation.post_id;
$$;

/** openapi:paths
/v1/fyp/feed-metrics:
  get:
    tags:
      - Feed
    summary: Feed quality metrics
    description: >-
      Aggregated feed-quality metrics from the telemetry rollup
      (feed_metrics_daily, #12), summed over a date range and grouped by
      algorithm_version / source / rank. Enables engagement-rate@rank, open
      rate, and average dwell/watch — and A/B comparison across
      algorithm_version. NON-AUTHORITATIVE: derived from the unauthenticated
      /v1/fyp/events log (username is client-claimed), so treat figures as
      directional and corroborate against on-chain engagement. Global-feed
      responses now carry the algorithm_version that produced them (#23), so
      clients that echo it back yield attributed global rows; the server never
      derives this column, so anything a client omits still lands in the ''
      bucket (feed_metrics_daily.algorithm_version is NOT NULL DEFAULT '').
      Rows recorded before #23 sit in '' for the same reason and are not
      backfilled. The global feed is identical for every viewer, so
      global-source attribution supports longitudinal before/after comparison
      rather than concurrent A/B arms.
    operationId: haf_fyp_endpoints.get_feed_metrics
    parameters:
      - in: query
        name: from_date
        required: false
        schema:
          x-sql-datatype: DATE
          type: [string, 'null']
          format: date
          default: null
        description: Inclusive start date (YYYY-MM-DD). Default 30 days ago.
      - in: query
        name: to_date
        required: false
        schema:
          x-sql-datatype: DATE
          type: [string, 'null']
          format: date
          default: null
        description: Inclusive end date (YYYY-MM-DD). Default today.
      - in: query
        name: algorithm_version
        required: false
        schema:
          type: [string, 'null']
          default: null
        description: Filter to one algorithm version (optional)
      - in: query
        name: source
        required: false
        schema:
          type: [string, 'null']
          default: null
        description: Filter to 'personalized' or 'global' (optional)
    responses:
      '200':
        description: Aggregated metric rows
        content:
          application/json:
            schema:
              x-sql-datatype: "jsonb"
              type: array
              items:
                type: object
                properties:
                  algorithm_version:
                    type: string
                  source:
                    type: string
                  rank:
                    type: integer
                  impressions:
                    type: integer
                  opens:
                    type: integer
                  actions:
                    type: integer
                  videos:
                    type: integer
                  engagement_rate:
                    type: number
                    nullable: true
                    description: actions (vote+comment+reblog+follow) / impressions
                  open_rate:
                    type: number
                    nullable: true
                    description: opens / impressions (closest to a click-through)
                  avg_dwell_ms:
                    type: number
                    nullable: true
                  avg_watch_ms:
                    type: number
                    nullable: true
 */
-- openapi-generated-code-begin
DROP FUNCTION IF EXISTS haf_fyp_endpoints.get_feed_metrics;
CREATE OR REPLACE FUNCTION haf_fyp_endpoints.get_feed_metrics(
    "from_date" DATE = NULL,
    "to_date" DATE = NULL,
    "algorithm_version" TEXT = NULL,
    "source" TEXT = NULL
)
RETURNS jsonb
-- openapi-generated-code-end
LANGUAGE sql STABLE
AS $$
    WITH agg AS (
        SELECT
            m.algorithm_version,
            m.source,
            m.rank,
            sum(m.impressions)  AS impressions,
            sum(m.opens)        AS opens,
            sum(m.actions)      AS actions,
            sum(m.videos)       AS videos,
            sum(m.dwell_ms_sum) AS dwell_ms_sum,
            sum(m.watch_ms_sum) AS watch_ms_sum
        FROM haf_fyp_app.feed_metrics_daily m
        WHERE m.day >= COALESCE(get_feed_metrics."from_date", now()::date - 30)
          AND m.day <= COALESCE(get_feed_metrics."to_date", now()::date)
          AND (get_feed_metrics."algorithm_version" IS NULL
               OR m.algorithm_version = get_feed_metrics."algorithm_version")
          AND (get_feed_metrics."source" IS NULL
               OR m.source = get_feed_metrics."source")
        GROUP BY m.algorithm_version, m.source, m.rank
    )
    SELECT COALESCE(
        jsonb_agg(
            jsonb_build_object(
                'algorithm_version', algorithm_version,
                'source', source,
                'rank', rank,
                'impressions', impressions,
                'opens', opens,
                'actions', actions,
                'videos', videos,
                'engagement_rate', CASE WHEN impressions > 0
                    THEN round(actions::numeric / impressions, 4) END,
                'open_rate', CASE WHEN impressions > 0
                    THEN round(opens::numeric / impressions, 4) END,
                'avg_dwell_ms', CASE WHEN impressions > 0
                    THEN round(dwell_ms_sum::numeric / impressions, 1) END,
                'avg_watch_ms', CASE WHEN videos > 0
                    THEN round(watch_ms_sum::numeric / videos, 1) END
            )
            ORDER BY algorithm_version, source, rank
        ),
        '[]'::jsonb
    )
    FROM agg;
$$;

GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA haf_fyp_endpoints TO haf_fyp_user;

RESET ROLE;
