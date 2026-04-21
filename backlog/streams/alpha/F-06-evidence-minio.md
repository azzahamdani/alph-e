---
id: F-06
subject: Evidence client — real MinIO + Postgres implementation
track: alpha
depends_on: []
advances_wi: [WI-003]
---

## Goal

Replace the `NotImplementedError` stubs in `agent.evidence.client.EvidenceClient`
with real I/O against MinIO (blobs) and Postgres (metadata). Honour the
write-order invariant: blob first, metadata second.

## Requirements

- `EvidenceClient.put_blob(...)` — writes to MinIO via boto3 (S3 API), then
  inserts the metadata row in `evidence_refs`. If the metadata insert fails,
  attempt a best-effort blob delete and surface the original error.
- `EvidenceClient.get_blob(...)` — reads from MinIO; raises `EvidenceNotFound`
  if missing.
- `EvidenceClient.record_metadata(...)` — direct insert; used by collectors
  that wrote the blob themselves (Go side via the C track).
- Postgres connections via `psycopg.AsyncConnection.connect`. No pool yet —
  the orchestrator is single-process for MVP1.
- Bootstrap the schema by running `migrations/0001_initial.sql` on first call
  if the table is missing (idempotent).

## Deliverables

- Replace stubs in `agent/src/agent/evidence/client.py`.
- New: `agent/src/agent/evidence/errors.py`.
- New: `agent/tests/integration/test_evidence_client.py` — runs against
  MinIO + Postgres in the `agent-infra` namespace, gated behind a
  `pytest -m integration` marker.

## Acceptance

- `mypy --strict` clean.
- `task agent-infra:install` has been run; `task agent-infra:postgres`
  and `task agent-infra:minio` are running in separate terminals;
  `task agent:test -- -m integration` passes.
- Integration test asserts: put → get round-trip; metadata row visible;
  best-effort blob delete on metadata failure (use a forced unique-violation).

## Guardrails

- Default credentials come from env (`EVIDENCE_S3_ENDPOINT`, `..._ACCESS_KEY`,
  `..._SECRET_KEY`, `EVIDENCE_S3_BUCKET`, `POSTGRES_URL`). No hard-coded creds
  even for dev — read from env, fail closed if absent.
- TTL enforcement is the bucket's lifecycle rule, not the client. Client only
  records `expires_at` for read-time advisory.

## Done signal

Flip `F-06` in [`../dependencies.md`](../dependencies.md) to `done`.
