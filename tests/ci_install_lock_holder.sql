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

-- A SHARED lock, on purpose: every block processor that depends on haf_fyp holds one
-- beside this, and only an installer asks for it exclusively. A processor that took it
-- exclusively would shut the others out -- and would still make the install skip, so
-- nothing later in the lane would notice.
DO $$
DECLARE
    _modes TEXT;
BEGIN
    SELECT string_agg(l.mode, ', ' ORDER BY l.mode) INTO _modes
    FROM pg_locks AS l WHERE l.pid = pg_backend_pid() AND l.locktype = 'advisory' AND l.granted;
    IF _modes IS NULL THEN
        RAISE EXCEPTION 'haf_fyp_app.take_block_processor_locks() took no lock: an install would run over a live block processor';
    END IF;
    -- ONE lock, because this database has no hivemind: with hivemind installed the
    -- processor rightly holds two, and this line is the one to change.
    IF _modes <> 'ShareLock' THEN
        RAISE EXCEPTION 'haf_fyp_app.take_block_processor_locks() holds % where a block processor holds one ShareLock', _modes;
    END IF;
END
$$;

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
