-- Evidence store metadata — see agent.evidence.client for invariants.
--
-- Blob storage is MinIO; this table is the metadata index the Postgres
-- checkpointer can join against. Lifecycle enforcement lives on the
-- MinIO bucket, not here — the `expires_at` column is advisory.

CREATE TABLE IF NOT EXISTS evidence_refs (
    evidence_id   TEXT PRIMARY KEY,
    incident_id   TEXT NOT NULL,
    storage_uri   TEXT NOT NULL,
    content_type  TEXT NOT NULL,
    size_bytes    BIGINT NOT NULL CHECK (size_bytes >= 0),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS evidence_refs_incident_idx
    ON evidence_refs (incident_id);

CREATE INDEX IF NOT EXISTS evidence_refs_expires_idx
    ON evidence_refs (expires_at);

-- Collector output cache — memoises calls on the key described in the arch
-- doc's 'Caching' section. Entries self-expire via `expires_at`; a background
-- sweep (post-MVP) reclaims rows.
CREATE TABLE IF NOT EXISTS collector_cache (
    cache_key     TEXT PRIMARY KEY,
    incident_id   TEXT NOT NULL,
    collector     TEXT NOT NULL,
    question      TEXT NOT NULL,
    finding_json  JSONB NOT NULL,
    evidence_id   TEXT NOT NULL REFERENCES evidence_refs (evidence_id)
                    ON DELETE CASCADE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS collector_cache_incident_idx
    ON collector_cache (incident_id);
