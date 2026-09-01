-- The least-privilege database role the two VPC Lambdas connect as.
--
-- Run once, from the EC2 app host (the only place with a network route to the
-- instance), as the RDS master user:
--
--     psql -v ON_ERROR_STOP=1 -f create_readonly_role.sql
--
-- Why this exists rather than the Lambdas using the master credentials:
--
--   * SQLExecutorTool's read-only guarantee came from SQLite's `mode=ro`, which
--     made writes physically impossible regardless of the query text. There is
--     no connection-string equivalent in PostgreSQL, so the guarantee has to be
--     rebuilt out of grants. That is what this file is.
--   * The master user is a member of rds_superuser and can create, drop, and
--     rewrite anything in the database. An LLM writes the SQL that runs through
--     these functions. Those two facts do not belong in the same session.
--   * No password. `GRANT rds_iam` makes this role authenticate only by IAM auth
--     token, so there is no credential to store, rotate, or leak into a public
--     repo -- and `PASSWORD NULL` means it cannot fall back to one.
--
-- Idempotent: safe to re-run after a schema reload.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'riskforge_ro') THEN
    CREATE ROLE riskforge_ro WITH LOGIN PASSWORD NULL;
  ELSE
    ALTER ROLE riskforge_ro WITH LOGIN PASSWORD NULL;
  END IF;
END
$$;

-- Authenticate by IAM auth token only.
GRANT rds_iam TO riskforge_ro;

-- Every transaction this role opens is read-only unless it explicitly asks
-- otherwise -- and the grants below mean asking otherwise gets it nowhere.
ALTER ROLE riskforge_ro SET default_transaction_read_only = on;

-- A runaway query (an accidental cross-join, say) should die in the server
-- rather than consume a Lambda's entire timeout and bill for the privilege.
ALTER ROLE riskforge_ro SET statement_timeout = '25s';
ALTER ROLE riskforge_ro SET idle_in_transaction_session_timeout = '30s';

-- Read the three tables, and nothing else. Note what is absent: no CREATE on
-- the schema, so the role cannot make itself a table to write into; no
-- privileges on future tables, so a later migration has to grant them
-- deliberately; and no pg_read_server_files, so COPY FROM has no local path to
-- reach for.
GRANT CONNECT ON DATABASE riskforge TO riskforge_ro;
GRANT USAGE ON SCHEMA public TO riskforge_ro;
GRANT SELECT ON loans, borrowers, risk_limits TO riskforge_ro;

REVOKE CREATE ON SCHEMA public FROM riskforge_ro;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO riskforge_ro;

-- Report what the role actually ended up with, so the run is verifiable rather
-- than assumed to have worked.
\echo '--- role attributes ---'
SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls
FROM pg_roles WHERE rolname = 'riskforge_ro';

\echo '--- session defaults ---'
SELECT rolname, rolconfig FROM pg_roles WHERE rolname = 'riskforge_ro';

\echo '--- table privileges (expect SELECT on three tables, nothing else) ---'
SELECT table_name, privilege_type
FROM information_schema.table_privileges
WHERE grantee = 'riskforge_ro'
ORDER BY table_name, privilege_type;

\echo '--- iam auth membership (expect rds_iam) ---'
SELECT r.rolname AS member, g.rolname AS granted_role
FROM pg_auth_members m
JOIN pg_roles r ON r.oid = m.member
JOIN pg_roles g ON g.oid = m.roleid
WHERE r.rolname = 'riskforge_ro';
