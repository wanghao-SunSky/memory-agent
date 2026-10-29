-- This cluster is local/demo-only. No database port is published to the host.
CREATE ROLE memory_app LOGIN PASSWORD 'memory-container-local';
CREATE DATABASE memory OWNER memory_app;
REVOKE CONNECT ON DATABASE memory FROM PUBLIC;
GRANT CONNECT ON DATABASE memory TO memory_app;
\connect memory
CREATE EXTENSION IF NOT EXISTS vector;
