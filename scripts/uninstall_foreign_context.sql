-- Which OTHER applications own something this teardown would destroy? (#66)
--
-- A file rather than an inline -c, for the reason scripts/uninstall_verify.sql is one:
-- a query that decides whether to destroy anything has to be executable on its own, and
-- tests/integration/test_uninstall_foreign_context_db.py runs exactly this text against a
-- real PostgreSQL. Pinned as a string inside the shell script it could only ever be
-- grepped, and a grep cannot tell `a.name <> 'haf_fyp'` from `a.name <> a.name`.
--
-- Prints one application name per line, or nothing. The caller refuses when it prints
-- anything; see GUARD 4 in scripts/uninstall_app.sh for what each step would do to them.
--
-- :context  the HAF context this uninstall targets (HAF_FYP_CONTEXT)
-- :schemas  comma-separated schemas this uninstall drops CASCADE (HAF_FYP_SCHEMAS)
--
-- Two destructive steps, keyed differently, so two disjuncts:
--
--   step 3  hive.app_remove_context(:context)  -- by context NAME. Strips the context
--           from the owning application's contexts array, and app_unregisters that
--           application outright when it was its only one.
--   step 4  DROP SCHEMA <schema> CASCADE       -- by SCHEMA. hive.context_remove ends in
--           `DROP TABLE %I.%I` (the context's schema, its name), so every context whose
--           tables live in a schema this script drops loses them here, whatever the
--           context is called.
--
-- `a.name <> 'haf_fyp'` excludes haf_fyp's OWN registration, which is what makes an
-- ordinary uninstall proceed. The application name is the literal haf_fyp on every node:
-- db/register_app.sql hardcodes it, while :context names the context, and only
-- scripts/uninstall_app.sh and docker/block-processing-healthcheck.sh honour a rename.
--
-- The JOIN means an application listing a context whose hafd.contexts row is already gone
-- does not match. That is deliberate: step 3 skips such a context (app_context_exists is
-- false) and step 4 has no table of its own to drop.
SELECT DISTINCT a.name
FROM hafd.applications AS a
INNER JOIN hafd.contexts AS c ON c.name = ANY(a.contexts)
WHERE a.name <> 'haf_fyp'
  AND (
      c.name = :'context'
      OR c.schema = ANY(STRING_TO_ARRAY(:'schemas', ','))
  )
ORDER BY a.name;
