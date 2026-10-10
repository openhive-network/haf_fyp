-- Stands in for a running block processor (#47, and #67 which the lock is for). Run by
-- the install lane's lock-holder service, on one connection that stays open.
--
-- It takes the lock the way the processor does -- haf_fyp_app.take_block_processor_locks,
-- as haf_fyp_owner -- and keeps the session alive until tests/ci_install_verify.sql has
-- seen what an install does meanwhile and the release-lock step has written a row to
-- ci_install_lane.lock_released. Advisory locks are session-level: they go when this
-- psql exits.
SET application_name = 'haf_fyp-ci-lock-holder';
SET ROLE haf_fyp_owner;
SELECT haf_fyp_app.take_block_processor_locks();
RESET ROLE;

DO $$
DECLARE
    _waited INT := 0;
BEGIN
    -- Each loop reads in its own statement snapshot; READ COMMITTED sees the row as
    -- soon as release-lock commits it.
    WHILE NOT EXISTS (SELECT 1 FROM ci_install_lane.lock_released) LOOP
        IF _waited >= 600 THEN
            RAISE EXCEPTION 'nobody released the lock within 120 s: the lane is stuck before release-lock';
        END IF;
        PERFORM pg_sleep(0.2);
        _waited := _waited + 1;
    END LOOP;
END
$$;
