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
  version: 1.0.0
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
  feed_post_row:
    type: object
    x-sql-datatype: object
    description: A single post entry in a feed result
    properties:
      post_id:
        type: integer
        description: Internal post identifier
      author:
        type: string
        description: Hive account name of the post author
      permlink:
        type: string
        description: Unique permlink of the post
      title:
        type: string
      community_id:
        type: integer
        description: Community the post belongs to (null if none)
      created_at:
        type: string
        format: date-time
      final_score:
        type: number
        description: Composite ranking score [0, 1]
      score_engagement:
        type: number
      score_recency:
        type: number
      score_credibility:
        type: number
      score_relevance:
        type: number
      community_boost_applied:
        type: boolean
      rank:
        type: integer
        description: Position in the feed (1-based)
      algorithm_version:
        type: string
      computed_at:
        type: string
        format: date-time
      personalized:
        type: boolean
        description: True if this row is from the user's own computed feed; false if it is the global fallback
 */

-- ---------------------------------------------------------------------------
-- root() - serves the full OpenAPI spec at GET /
-- ---------------------------------------------------------------------------

DO $__$
DECLARE
    swagger_url TEXT;
BEGIN
    swagger_url := COALESCE(current_setting('custom.swagger_url', TRUE), 'localhost');

    EXECUTE $exec$
CREATE OR REPLACE FUNCTION haf_fyp_endpoints.root() RETURNS json
LANGUAGE sql STABLE
AS $func$
    SELECT $api_json$
{
  "openapi": "3.1.0",
  "info": {
    "title": "HAF FYP - For You Page",
    "description": "Personalized content feed API for the Hive blockchain. Provides algorithmically ranked post feeds, user profiles, and algorithm version management.",
    "license": {
      "name": "MIT License",
      "url": "https://opensource.org/license/mit"
    },
    "version": "1.0.0"
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
      "feed_post_row": {
        "type": "object",
        "x-sql-datatype": "object",
        "description": "A single post entry in a feed result",
        "properties": {
          "post_id": {
            "type": "integer",
            "description": "Internal post identifier"
          },
          "author": {
            "type": "string",
            "description": "Hive account name of the post author"
          },
          "permlink": {
            "type": "string",
            "description": "Unique permlink of the post"
          },
          "title": {
            "type": "string"
          },
          "community_id": {
            "type": "integer",
            "description": "Community the post belongs to (null if none)"
          },
          "created_at": {
            "type": "string",
            "format": "date-time"
          },
          "final_score": {
            "type": "number",
            "description": "Composite ranking score [0, 1]"
          },
          "score_engagement": {
            "type": "number"
          },
          "score_recency": {
            "type": "number"
          },
          "score_credibility": {
            "type": "number"
          },
          "score_relevance": {
            "type": "number"
          },
          "community_boost_applied": {
            "type": "boolean"
          },
          "rank": {
            "type": "integer",
            "description": "Position in the feed (1-based)"
          },
          "algorithm_version": {
            "type": "string"
          },
          "computed_at": {
            "type": "string",
            "format": "date-time"
          },
          "personalized": {
            "type": "boolean",
            "description": "True if this row is from the user's own computed feed; false if it is the global fallback"
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
        "description": "Returns a ranked list of posts for the given user, ordered by final score.",
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
            "name": "limit",
            "required": false,
            "schema": {
              "type": "integer",
              "default": 20
            },
            "description": "Maximum number of posts to return"
          },
          {
            "in": "query",
            "name": "offset",
            "required": false,
            "schema": {
              "type": "integer",
              "default": 0
            },
            "description": "Pagination offset"
          }
        ],
        "responses": {
          "200": {
            "description": "Ranked post list (empty array before first ranker cycle)",
            "content": {
              "application/json": {
                "schema": {
                  "x-sql-datatype": "SETOF haf_fyp_endpoints.feed_post_row",
                  "type": "array",
                  "items": {
                    "$ref": "#/components/schemas/feed_post_row"
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
        "description": "Returns the global unauthenticated feed, optionally filtered by community.",
        "operationId": "haf_fyp_endpoints.get_global_feed",
        "parameters": [
          {
            "in": "query",
            "name": "limit",
            "required": false,
            "schema": {
              "type": "integer",
              "default": 20
            }
          },
          {
            "in": "query",
            "name": "offset",
            "required": false,
            "schema": {
              "type": "integer",
              "default": 0
            }
          },
          {
            "in": "query",
            "name": "community_id",
            "required": false,
            "schema": {
              "type": "integer",
              "default": null
            },
            "description": "Filter results to a specific community (optional)"
          }
        ],
        "responses": {
          "200": {
            "description": "Global ranked post list",
            "content": {
              "application/json": {
                "schema": {
                  "x-sql-datatype": "SETOF haf_fyp_endpoints.feed_post_row",
                  "type": "array",
                  "items": {
                    "$ref": "#/components/schemas/feed_post_row"
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
                  "x-sql-datatype": "TABLE(post_id bigint, author varchar(16), permlink varchar(256), title text, final_score float, score_relevance float, score_recency float, score_engagement float, score_credibility float, community_boost_applied boolean, rank int, algorithm_version varchar(16), algorithm_weights jsonb, computed_at timestamp)",
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
  }
}
$api_json$::json;
$func$;
$exec$;
END
$__$;

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
-- Personalized feed
-- ---------------------------------------------------------------------------

/** openapi:paths
/v1/fyp/feed/{username}:
  get:
    tags:
      - Feed
    summary: Personalized feed
    description: Returns a ranked list of posts for the given user, ordered by final score.
    operationId: haf_fyp_endpoints.get_personalized_feed
    parameters:
      - in: path
        name: username
        required: true
        schema:
          type: string
        description: Hive account name
      - in: query
        name: limit
        required: false
        schema:
          type: integer
          default: 20
        description: Maximum number of posts to return
      - in: query
        name: offset
        required: false
        schema:
          type: integer
          default: 0
        description: Pagination offset
    responses:
      '200':
        description: Ranked post list (empty array before first ranker cycle)
        content:
          application/json:
            schema:
              x-sql-datatype: "SETOF haf_fyp_endpoints.feed_post_row"
              type: array
              items:
                $ref: '#/components/schemas/feed_post_row'
 */
-- openapi-generated-code-begin
DROP FUNCTION IF EXISTS haf_fyp_endpoints.get_personalized_feed;
CREATE OR REPLACE FUNCTION haf_fyp_endpoints.get_personalized_feed(
    "username" TEXT,
    "limit" INT = 20,
    "offset" INT = 0
)
RETURNS SETOF haf_fyp_endpoints.feed_post_row 
-- openapi-generated-code-end
LANGUAGE sql STABLE
AS $$
    -- Cold-start fallback: users without a precomputed personalized feed
    -- (no interactions yet, or never scored) get the global feed instead of an
    -- empty result, so the "For You" page is never blank. Personalization kicks
    -- in automatically once the user has a cached feed.
    WITH has_personal AS (
        SELECT EXISTS (
            SELECT 1 FROM haf_fyp_app.feed_cache
            WHERE username = get_personalized_feed.username
        ) AS yes
    )
    -- Personalized feed
    SELECT
        fc.post_id,
        pc.author,
        pc.permlink,
        pc.title,
        pc.community_id,
        pc.created_at,
        fc.final_score,
        fc.score_engagement,
        fc.score_recency,
        fc.score_credibility,
        fc.score_relevance,
        fc.community_boost_applied,
        fc.rank,
        fc.algorithm_version,
        fc.computed_at,
        TRUE AS personalized
    FROM haf_fyp_app.feed_cache fc
    JOIN haf_fyp_app.post_candidates pc ON pc.post_id = fc.post_id
    WHERE fc.username = get_personalized_feed.username
      AND (SELECT yes FROM has_personal)

    UNION ALL

    -- Global feed fallback (only when the user has no personalized feed)
    SELECT
        gfc.post_id,
        pc.author,
        pc.permlink,
        pc.title,
        pc.community_id,
        pc.created_at,
        gfc.final_score,
        gfc.score_engagement,
        gfc.score_recency,
        gfc.score_credibility,
        NULL::float AS score_relevance,
        FALSE AS community_boost_applied,
        gfc.rank,
        NULL::varchar(16) AS algorithm_version,
        gfc.computed_at,
        FALSE AS personalized
    FROM haf_fyp_app.global_feed_cache gfc
    JOIN haf_fyp_app.post_candidates pc ON pc.post_id = gfc.post_id
    WHERE NOT (SELECT yes FROM has_personal)
    ORDER BY rank ASC
    LIMIT get_personalized_feed."limit"
    OFFSET get_personalized_feed."offset";
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
    description: Returns the global unauthenticated feed, optionally filtered by community.
    operationId: haf_fyp_endpoints.get_global_feed
    parameters:
      - in: query
        name: limit
        required: false
        schema:
          type: integer
          default: 20
      - in: query
        name: offset
        required: false
        schema:
          type: integer
          default: 0
      - in: query
        name: community_id
        required: false
        schema:
          type: integer
          default: null
        description: Filter results to a specific community (optional)
    responses:
      '200':
        description: Global ranked post list
        content:
          application/json:
            schema:
              x-sql-datatype: "SETOF haf_fyp_endpoints.feed_post_row"
              type: array
              items:
                $ref: '#/components/schemas/feed_post_row'
 */
-- openapi-generated-code-begin
DROP FUNCTION IF EXISTS haf_fyp_endpoints.get_global_feed;
CREATE OR REPLACE FUNCTION haf_fyp_endpoints.get_global_feed(
    "limit" INT = 20,
    "offset" INT = 0,
    "community_id" INT = NULL
)
RETURNS SETOF haf_fyp_endpoints.feed_post_row 
-- openapi-generated-code-end
LANGUAGE sql STABLE
AS $$
    SELECT
        gfc.post_id,
        pc.author,
        pc.permlink,
        pc.title,
        pc.community_id,
        pc.created_at,
        gfc.final_score,
        gfc.score_engagement,
        gfc.score_recency,
        gfc.score_credibility,
        NULL::float AS score_relevance,
        FALSE AS community_boost_applied,
        gfc.rank,
        NULL::varchar(16) AS algorithm_version,
        gfc.computed_at,
        FALSE AS personalized
    FROM haf_fyp_app.global_feed_cache gfc
    JOIN haf_fyp_app.post_candidates pc ON pc.post_id = gfc.post_id
    WHERE (get_global_feed.community_id IS NULL OR pc.community_id = get_global_feed.community_id)
    ORDER BY gfc.rank ASC
    LIMIT get_global_feed."limit"
    OFFSET get_global_feed."offset";
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
              x-sql-datatype: "TABLE(post_id bigint, author varchar(16), permlink varchar(256), title text, final_score float, score_relevance float, score_recency float, score_engagement float, score_credibility float, community_boost_applied boolean, rank int, algorithm_version varchar(16), algorithm_weights jsonb, computed_at timestamp)"
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
RETURNS TABLE(post_id bigint, author varchar(16), permlink varchar(256), title text, final_score float, score_relevance float, score_recency float, score_engagement float, score_credibility float, community_boost_applied boolean, rank int, algorithm_version varchar(16), algorithm_weights jsonb, computed_at timestamp) 
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

GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA haf_fyp_endpoints TO haf_fyp_user;

RESET ROLE;
