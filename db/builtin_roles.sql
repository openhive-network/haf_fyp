DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'haf_fyp_owner') THEN
        CREATE ROLE haf_fyp_owner NOLOGIN NOINHERIT;
    END IF;

    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'haf_fyp_user') THEN
        CREATE ROLE haf_fyp_user NOLOGIN NOINHERIT;
    END IF;
END
$$;

-- haf_fyp_user is readable by haf_fyp_owner
GRANT haf_fyp_user TO haf_fyp_owner;

-- haf_admin (HAF superrole) owns haf_fyp_owner
GRANT haf_fyp_owner TO haf_admin;
