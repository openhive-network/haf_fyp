DO $$
BEGIN
  CREATE ROLE haf_fyp_owner WITH LOGIN INHERIT IN ROLE hive_applications_owner_group;
EXCEPTION WHEN duplicate_object THEN RAISE NOTICE '%, skipping', SQLERRM USING ERRCODE = SQLSTATE;
END
$$;

DO $$
BEGIN
  CREATE ROLE haf_fyp_user WITH LOGIN INHERIT IN ROLE hive_applications_group;
EXCEPTION WHEN duplicate_object THEN RAISE NOTICE '%, skipping', SQLERRM USING ERRCODE = SQLSTATE;
END
$$;

-- Allow haf_admin to act as haf_fyp_owner (for installation scripts)
GRANT haf_fyp_owner TO haf_admin;

-- Allow haf_fyp_owner to act as haf_fyp_user (for testing)
GRANT haf_fyp_user TO haf_fyp_owner;
