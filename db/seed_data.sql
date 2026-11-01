-- Seed data: algorithm versions.
-- Safe to re-run (ON CONFLICT DO NOTHING). The newest version is the active one.

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

-- 1.3.0 (#116): the 1.1.0 weights, with the boosted feed rescaled into [0, 1] instead of
-- clipped at 1, which tied the top of every boosted feed. It takes over from 1.1.0 only
-- on the install that registers it, and only where 1.1.0 is the one active version: an
-- operator who chose another version keeps it, and one who goes back to 1.1.0 later is
-- not rolled forward again by the next install.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM haf_fyp_app.algorithm_versions WHERE version = '1.3.0') THEN
        INSERT INTO haf_fyp_app.algorithm_versions (version, description, weights, deployed_at, is_active)
        VALUES (
            '1.3.0',
            'Tiered community boost as in 1.1.0, but a boosted feed is rescaled into [0, 1] '
            'instead of clipped at 1, so its top keeps its order (#116)',
            '{
                "relevance":               0.40,
                "recency":                 0.30,
                "engagement":              0.20,
                "credibility":             0.10,
                "community_boost":         0.20,
                "community_boost_engaged": 0.10,
                "boost_rescale":           true
            }'::jsonb,
            -- Later than the NOW() of 1.2.0 above even when both run in one
            -- transaction: list_algorithm_versions orders by deployed_at.
            CLOCK_TIMESTAMP(),
            -- TRUE only when 1.1.0 is active and no other version is.
            COALESCE((SELECT BOOL_AND(v.version = '1.1.0') FROM haf_fyp_app.algorithm_versions AS v
                      WHERE v.is_active), FALSE)
        );
        UPDATE haf_fyp_app.algorithm_versions
        SET is_active = FALSE
        WHERE version = '1.1.0'
          AND (SELECT n.is_active FROM haf_fyp_app.algorithm_versions AS n WHERE n.version = '1.3.0');
    END IF;
END
$$;
