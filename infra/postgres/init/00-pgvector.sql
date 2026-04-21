-- Enable pgvector at cluster init so later migrations can use vector(n) types.
CREATE EXTENSION IF NOT EXISTS vector;
