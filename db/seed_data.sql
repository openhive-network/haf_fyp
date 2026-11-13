-- Seed data: algorithm versions.
-- Safe to re-run (ON CONFLICT DO NOTHING; 1.3.0, 1.4.0 and 1.4.1 only when missing).
-- A fresh install ends with 1.4.1 active (1.3.0 takes over from 1.1.0, then 1.4.1 from 1.3.0).

INSERT INTO haf_fyp_app.algorithm_versions (version, description, weights, deployed_at, is_active)
VALUES (
    '1.0.0',
    'Initial release - 4-signal weighted sum with single community boost multiplier',
    '{
        "relevance":       0.40,
        "recency":         0.30,
        "engagement":      0.20,
        "credibility":     0.10,
        "community_boost": 0.20
    }'::jsonb,
    NOW() - interval '1 day',
    FALSE
),
(
    '1.1.0',
    'Tiered community boost: full for subscribed/declared communities, smaller for engaged-only',
    '{
        "relevance":               0.40,
        "recency":                 0.30,
        "engagement":              0.20,
        "credibility":             0.10,
        "community_boost":         0.20,
        "community_boost_engaged": 0.10
    }'::jsonb,
    NOW() - interval '1 hour',
    TRUE
),
(
    '1.2.0',
    'Diversity experiment (#11): softened community boost (0.20 -> 0.08 / 0.10 -> 0.04) '
    'plus a per-page single-community cap. Registered INACTIVE — activate via '
    'POST /v1/fyp/admin/algorithm to A/B it against 1.1.0 on feed_metrics_daily.',
    '{
        "relevance":               0.40,
        "recency":                 0.30,
        "engagement":              0.20,
        "credibility":             0.10,
        "community_boost":         0.08,
        "community_boost_engaged": 0.04,
        "max_per_community":       8
    }'::jsonb,
    NOW(),
    FALSE
)
ON CONFLICT (version) DO NOTHING;

-- 1.3.0 (#116): 1.1.0 plus boost_rescale, which divides every score by 1 + the larger
-- boost instead of clipping a boosted score at 1 (the clip tied the top of every boosted
-- feed). Its weights are this node's 1.1.0 weights, so an operator's tuning carries over
-- (on a fresh install, the ones above). It takes over from 1.1.0 only on the install
-- that registers it, and only where 1.1.0 is the one active version: an operator who
-- chose another version keeps it, and one who goes back to 1.1.0 later is not rolled
-- forward again by the next install.
DO $$
DECLARE
    _take_over boolean;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM haf_fyp_app.algorithm_versions WHERE version = '1.3.0') THEN
        -- TRUE only when 1.1.0 is active and no other version is.
        SELECT COALESCE(BOOL_AND(v.version = '1.1.0'), FALSE) INTO _take_over
        FROM haf_fyp_app.algorithm_versions AS v
        WHERE v.is_active;

        INSERT INTO haf_fyp_app.algorithm_versions (version, description, weights, deployed_at, is_active)
        VALUES (
            '1.3.0',
            'Tiered community boost as in 1.1.0, but every score is divided by 1 + the larger '
            'boost instead of a boosted score being clipped at 1, so the top of a feed keeps '
            'its order (#116)',
            -- The statement above has just made sure a 1.1.0 row exists.
            (SELECT v.weights FROM haf_fyp_app.algorithm_versions AS v WHERE v.version = '1.1.0')
            || '{"boost_rescale": true}'::jsonb,
            -- Later than the NOW() of 1.2.0 above even when both run in one
            -- transaction: list_algorithm_versions orders by deployed_at.
            CLOCK_TIMESTAMP(),
            _take_over
        );

        IF _take_over THEN
            UPDATE haf_fyp_app.algorithm_versions SET is_active = FALSE WHERE version = '1.1.0';
        END IF;
    END IF;
END
$$;

-- 1.4.0 (#117): 1.3.0 plus reader-to-author affinity, which raises relevance for the
-- authors a reader has lately voted on, reblogged or replied to (haf_fyp/affinity.py,
-- scoring.compute_scores). Registered INACTIVE: activate it with
-- POST /v1/fyp/admin/algorithm to compare it with 1.3.0 on feed_metrics_daily, once the
-- ranker runs this release. Its weights are this node's 1.3.0 weights when it is
-- registered, so the tuning made until then carries over (not later changes to 1.3.0).
INSERT INTO haf_fyp_app.algorithm_versions (version, description, weights, deployed_at, is_active)
SELECT
    '1.4.0' AS version,
    'As 1.3.0, plus reader-to-author affinity: relevance is raised for authors the '
    'reader voted on, reblogged or replied to in the last 30 days, decayed by half '
    'each week (#117)' AS description,
    v.weights || '{
        "author_affinity":         0.30,
        "affinity_days":           30,
        "affinity_half_life_days": 7,
        "affinity_half_at":        3,
        "affinity_max_authors":    200
    }'::jsonb AS weights,
    -- After 1.3.0 even within one transaction: list_algorithm_versions orders by it.
    CLOCK_TIMESTAMP() AS deployed_at,
    FALSE AS is_active
FROM haf_fyp_app.algorithm_versions AS v
WHERE v.version = '1.3.0'
ON CONFLICT (version) DO NOTHING;

-- 1.4.1 (#117): 1.4.0 with author_affinity 0.60 (1.4.0 ships 0.30). A dry run of 1.4.0
-- on a live node kept the same 20 posts in most top-20s: relevance there is already
-- about 0.7, so 0.30 of the headroom moves little. Its weights are this node's 1.4.0
-- weights when it is registered, with the share set to 0.60.
-- 1.4.1 is the default: it takes over from 1.3.0 the way 1.3.0 takes over from 1.1.0,
-- only on the install that registers it and only where 1.3.0 is the one active version
-- (so a fresh install, and a node still on the shipped default, end on 1.4.1). A node
-- that registered 1.4.1 inactive with an earlier release keeps its active version: the
-- seed cannot tell an operator's choice of 1.3.0 from the old default. Measured against
-- 1.3.0 on testapi in #117: no measurable difference in opens or upvotes.
DO $$
DECLARE
    _take_over boolean;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM haf_fyp_app.algorithm_versions WHERE version = '1.4.1') THEN
        -- TRUE only when 1.3.0 is active and no other version is.
        SELECT COALESCE(BOOL_AND(v.version = '1.3.0'), FALSE) INTO _take_over
        FROM haf_fyp_app.algorithm_versions AS v
        WHERE v.is_active;

        INSERT INTO haf_fyp_app.algorithm_versions (version, description, weights, deployed_at, is_active)
        SELECT
            '1.4.1',
            'As 1.4.0, with 0.60 of relevance headroom given to reader-to-author affinity '
            '(1.4.0 ships 0.30) (#117)',
            v.weights || '{"author_affinity": 0.60}'::jsonb,
            -- After 1.4.0 even within one transaction: list_algorithm_versions orders by it.
            CLOCK_TIMESTAMP(),
            _take_over
        FROM haf_fyp_app.algorithm_versions AS v
        WHERE v.version = '1.4.0';

        IF _take_over THEN
            UPDATE haf_fyp_app.algorithm_versions SET is_active = FALSE WHERE version = '1.3.0';
        END IF;
    END IF;
END
$$;
