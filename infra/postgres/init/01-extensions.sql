-- Runs once, on first initialization of an empty data volume.
-- pgvector backs the failure-trace clustering added in P4.
CREATE EXTENSION IF NOT EXISTS vector;
