#!/bin/sh
# Runs only when PostgreSQL initializes a new data volume.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=agent_password="$AGENT_DB_PASSWORD" <<'SQL'
CREATE ROLE agent LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'agent_password';
ALTER DATABASE agent OWNER TO agent;
ALTER SCHEMA public OWNER TO agent;
SQL
