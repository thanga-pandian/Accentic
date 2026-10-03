-- Run as postgres superuser to set up the local dev database
-- psql -U postgres -f db/setup_local_postgres.sql

CREATE USER assent WITH PASSWORD 'assent';
CREATE DATABASE assent_compliance OWNER assent;
GRANT ALL PRIVILEGES ON DATABASE assent_compliance TO assent;
