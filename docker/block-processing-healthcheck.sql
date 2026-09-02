-- Block-processing liveness predicate for docker/block-processing-healthcheck.sh (#27).
-- Returns exactly one boolean; the shell compares it to 't'.
--
-- IN A FILE, not interpolated into a shell string, for two reasons.
--
-- 1. It is the only way to test it. While this lived in the shell, the tests could
--    only grep the assembled command for substrings -- and a substring search cannot
--    tell `a - b` from `b - a`. Swapping the operands of the subtraction below makes
--    the difference permanently negative, so the freshness test is always true and a
--    wedged main() reports healthy forever; every assertion still passed. As a file
--    the whole expression can be compared against an expected one, which no operand
--    swap, prepended tautology or wrapped call survives.
-- 2. psql's :'var' substitution quotes and escapes, so nothing here is built by string
--    concatenation. The shell still validates :max_age, but as belt-and-braces rather
--    than as the only defence.
--
-- Parameters, all supplied with -v by the caller:
--   context       HAF context name (haf_fyp)
--   max_age       whole seconds the loop may go without iterating
--   startup_time  when THIS container started processing, from the server's clock
--
-- NO `SET TIME ZONE`. last_active_at is `timestamp WITHOUT time zone`, written by HAF
-- as NOW() in the WRITER SESSION's zone. Upstream forces the reader to UTC, which does
-- not convert that value -- it only makes the reader assume it is UTC -- so on a server
-- ahead of UTC the difference goes negative and the freshness test passes forever.
-- Reading with now()::timestamp instead keeps both operands in one frame.
--
-- Be precise about WHICH frame, because an earlier revision claimed this "removes the
-- assumption entirely" and it does not: now()::timestamp renders in the SESSION's
-- TimeZone, not the server's. What makes it correct is that writer and reader resolve
-- to the same setting -- true here because both run in the same container against the
-- same server default. A per-role `ALTER ROLE ... SET TimeZone`, a per-database
-- setting, or PGTZ in one context and not the other reintroduces the identical
-- fail-open; so does a server zone that observes DST, across a fall-back transition.
-- Measured on this deployment: the server runs Etc/UTC, which has neither problem.
--
-- scripts/process_blocks.sh takes startup_time from the same server clock for the same
-- reason, so all three values are commensurable.

SELECT
    (
        -- The loop iterated recently...
        (now()::timestamp - (SELECT c.last_active_at FROM hafd.contexts AS c WHERE c.name = :'context'))
            < (:'max_age' || ' seconds')::interval
        -- ...and did so in THIS container, not before a restart.
        AND (SELECT c.last_active_at FROM hafd.contexts AS c WHERE c.name = :'context')
            > :'startup_time'::timestamp
    )
    -- Or there is simply nothing left to process. Upstream ships this arm and it is
    -- kept for parity, but it is an OR: it can report healthy without the freshness
    -- test passing, including right after a restart of an in-sync app and when hived
    -- and the loop freeze together. See the script header.
    OR hive.is_app_in_sync(:'context');
