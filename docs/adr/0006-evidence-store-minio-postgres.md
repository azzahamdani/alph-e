# ADR-0006: Evidence store — MinIO blobs + Postgres metadata

- Status: accepted
- Date: 2026-04-21

## Context

The architecture's evidence store contract is: raw observability payloads live at an opaque URI; agents hold only `EvidenceRef { evidence_id, storage_uri, content_type, size_bytes, expires_at }` and dereference on demand. This requires:

- An object store with lifecycle rules (TTL).
- A metadata catalog (lookup by `evidence_id`, filter by incident, enumerate for GC, hold `content_type`/`size_bytes`/`expires_at`).
- Local-dev simplicity — we don't want real S3 for a lab.

## Decision

- **Blobs**: MinIO, running in `infra/docker-compose.yaml`. Bucket `incidents`. 30-day lifecycle rule (matches the arch doc's default `EvidenceRef.expires_at` cadence).
  - Storage URI format: `s3://incidents/<evidence_id>.<ext>`. Kept S3-scheme so swapping for real S3 later is a config change, not a code change.
  - Access via `boto3` from Python, `github.com/minio/minio-go/v7` from Go. Both use signature v4 against MinIO's S3-compatible endpoint.
- **Metadata**: Postgres (already running for LangGraph's `PostgresSaver` checkpointer). Schema lives at `agent/src/agent/evidence/migrations/0001_initial.sql` with tables:
  - `incidents` — one row per IncidentState, holds high-level fields and a JSONB snapshot.
  - `evidence` — (`evidence_id`, `incident_id`, `collector_name`, `storage_uri`, `content_type`, `size_bytes`, `expires_at`, `created_at`). `incident_id` indexed.
  - `actions_taken` — audit log for `ActionIntent` / `Action` pairs (arch doc safety contract).
- **TTL enforcement**: two belts. (1) MinIO lifecycle rule physically deletes blobs after 30 days. (2) A Postgres delete job sweeps metadata rows with `expires_at < now()` — runs opportunistically from the Coordinator once per incident close. MinIO is authoritative for "is the blob there?"

## Consequences

- **+** Both halves run in the same `docker-compose up`. One command gets the whole agent-side infra.
- **+** S3-scheme URIs mean the real-cloud path is a config swap (`EVIDENCE_S3_ENDPOINT`). No code diff.
- **+** Postgres does double duty (evidence metadata + LangGraph checkpoints), which keeps the data-plane surface narrow.
- **−** MinIO in single-node mode isn't HA. Fine for MVP1; production gets real S3.
- **−** Lifecycle-rule ≠ perfect TTL. MinIO sweeps are periodic (default daily). For correctness against `expires_at`, read-before-dereference checks `EvidenceRef.expires_at` and treats "expired but not yet GC'd" as missing.
- Evidence writes MUST commit **before** the LangGraph checkpoint that references them (per arch doc ordering invariant). Enforced in the orchestrator's collector node.
