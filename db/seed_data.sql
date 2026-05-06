-- Seed data: initial algorithm version.
-- Safe to re-run (ON CONFLICT DO NOTHING).

INSERT INTO haf_fyp_app.algorithm_versions (version, description, weights, deployed_at, is_active)
VALUES (
    '1.0.0',
    'Initial release — 4-signal weighted sum with community boost multiplier',
    '{
        "relevance":       0.40,
        "recency":         0.30,
        "engagement":      0.20,
        "credibility":     0.10,
        "community_boost": 0.20
    }'::jsonb,
    NOW(),
    TRUE
)
ON CONFLICT (version) DO NOTHING;
