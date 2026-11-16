\set ON_ERROR_STOP on
BEGIN READ ONLY;
SELECT current_database() AS database, current_user AS role, current_setting('server_version') AS version;
SELECT extname, extversion FROM pg_extension ORDER BY extname;
SELECT schema_name FROM information_schema.schemata WHERE schema_name NOT LIKE 'pg_%' ORDER BY schema_name;
SELECT table_schema, count(*) AS tables FROM information_schema.tables WHERE table_type = 'BASE TABLE' AND table_schema NOT IN ('information_schema','pg_catalog') GROUP BY table_schema ORDER BY table_schema;
SELECT has_database_privilege(current_user,current_database(),'CONNECT') AS can_connect;
ROLLBACK;
