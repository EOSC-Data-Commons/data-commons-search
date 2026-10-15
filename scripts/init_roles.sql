-- Roles of the search API and of the ingestion, their access to the databases, and the extensions of appdb.
-- Run by a superuser connected to appdb. Temporary, until the metadata-warehouse manages the users.
-- Then the populate_appdb_dataset DAG creates the tables as the ingest role (create_appdb_tables task),
-- the default privileges below give the search role access to them. No role management in the python code.
--
--   psql -d appdb -v ON_ERROR_STOP=1 -v search_user=app -v search_password=... \
--     -v ingest_user=appdb_ingest -v ingest_password=... -f scripts/init_roles.sql
--
-- search role (the API): reads and writes the rows of all the appdb tables (only owners can alter, truncate or drop them)
-- ingest role (the DAGs and scripts): reads all the warehouse databases, creates (so owns) and writes the appdb tables
-- Idempotent, existing roles keep their password (change it with ALTER USER).

-- Login roles, when missing
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'search_user', :'search_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'search_user') \gexec
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'ingest_user', :'ingest_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'ingest_user') \gexec

-- Extensions of the search tables (dataset_models.py), vectorscale needs a superuser
CREATE EXTENSION IF NOT EXISTS vectorscale CASCADE;
CREATE EXTENSION IF NOT EXISTS pg_textsearch;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;

-- Access to appdb. The ingest role creates the tables, and writes the ones created before by another role
SELECT format('GRANT CONNECT ON DATABASE %I TO %I, %I', current_database(), :'search_user', :'ingest_user') \gexec
GRANT USAGE ON SCHEMA public TO :"search_user";
GRANT USAGE, CREATE ON SCHEMA public TO :"ingest_user";
GRANT ALL ON ALL TABLES IN SCHEMA public TO :"ingest_user";
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO :"ingest_user";

-- Tables, views and functions created before by another role (e.g. the admin) go to the ingest role, which
-- must own them to index, alter and refresh them. Their sequences and indexes follow, extension objects are left
SELECT format('ALTER %s %s OWNER TO %I',
    CASE c.relkind WHEN 'm' THEN 'MATERIALIZED VIEW' WHEN 'v' THEN 'VIEW' ELSE 'TABLE' END,
    CAST(c.oid AS regclass), :'ingest_user')
FROM pg_class c
WHERE c.relnamespace = CAST('public' AS regnamespace) AND c.relkind IN ('r', 'p', 'v', 'm')
    AND pg_get_userbyid(c.relowner) <> :'ingest_user'
    AND NOT EXISTS (SELECT FROM pg_depend d WHERE d.objid = c.oid AND d.deptype = 'e') \gexec
SELECT format('ALTER FUNCTION %s OWNER TO %I', CAST(p.oid AS regprocedure), :'ingest_user')
FROM pg_proc p
WHERE p.pronamespace = CAST('public' AS regnamespace) AND pg_get_userbyid(p.proowner) <> :'ingest_user'
    AND NOT EXISTS (SELECT FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e') \gexec

-- Access of the search role to the existing tables, and to the ones the ingest role creates later
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"search_user";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"search_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"ingest_user" IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"search_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"ingest_user" IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO :"search_user";

-- Read access of the ingest role to the warehouse databases, also to the tables this admin creates later
SELECT EXISTS (SELECT FROM pg_database WHERE datname = 'datasetdb') AS has_datasetdb,
    EXISTS (SELECT FROM pg_database WHERE datname = 'tooldb') AS has_tooldb,
    EXISTS (SELECT FROM pg_database WHERE datname = 'filedb') AS has_filedb \gset

\if :has_datasetdb
\c datasetdb
GRANT CONNECT ON DATABASE datasetdb TO :"ingest_user";
GRANT USAGE ON SCHEMA public TO :"ingest_user";
GRANT SELECT ON ALL TABLES IN SCHEMA public TO :"ingest_user";
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO :"ingest_user";
\endif

\if :has_tooldb
\c tooldb
GRANT CONNECT ON DATABASE tooldb TO :"ingest_user";
GRANT USAGE ON SCHEMA public TO :"ingest_user";
GRANT SELECT ON ALL TABLES IN SCHEMA public TO :"ingest_user";
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO :"ingest_user";
\endif

\if :has_filedb
\c filedb
GRANT CONNECT ON DATABASE filedb TO :"ingest_user";
GRANT USAGE ON SCHEMA public TO :"ingest_user";
GRANT SELECT ON ALL TABLES IN SCHEMA public TO :"ingest_user";
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO :"ingest_user";
\endif
