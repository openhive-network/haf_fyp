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
    NOW() - INTERVAL '1 day',
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
    NOW(),
    TRUE
)
ON CONFLICT (version) DO NOTHING;
