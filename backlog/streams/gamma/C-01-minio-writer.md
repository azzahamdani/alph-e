---
id: C-01
subject: MinIO-backed evidence Writer
track: gamma
depends_on: []
advances_wi: [WI-005, WI-006, WI-007]
---

## Goal

Replace `evidence.NullWriter` with a real `MinioWriter` that writes blobs to
MinIO via the AWS S3 SDK and returns a populated `contract.EvidenceRef`.

## Requirements

- New type `evidence.MinioWriter` implementing the `Writer` interface.
  Constructor: `NewMinioWriter(ctx, MinioConfig) (*MinioWriter, error)`.
- `MinioConfig` reads from env: `EVIDENCE_S3_ENDPOINT`, `EVIDENCE_S3_ACCESS_KEY`,
  `EVIDENCE_S3_SECRET_KEY`, `EVIDENCE_S3_BUCKET`, optional
  `EVIDENCE_S3_REGION` (default `us-east-1`).
- `Put` writes the blob with `PutObject` and returns the `EvidenceRef` whose
  `StorageURI` is `s3://<bucket>/<evidence_id>` and whose `ExpiresAt` is
  `time.Now().Add(DefaultTTL)`.
- Bucket bootstrap is **not** the writer's job — `infra:up` already creates
  the bucket and lifecycle rule. Writer assumes the bucket exists; surface
  a typed error otherwise.

## Deliverables

- `collectors/internal/evidence/minio_writer.go`
- `collectors/internal/evidence/minio_writer_test.go` — unit tests using a
  mocked S3 client interface (extract a small `s3API` interface so the test
  doesn't need network).

## Acceptance

- `golangci-lint run ./...` clean.
- `go test ./...` passes (no network).
- Wire `cmd/prom-collector/main.go`, `cmd/loki-collector/main.go`, and
  `cmd/kube-collector/main.go` to use `MinioWriter` when env vars are
  present, falling back to `NullWriter` otherwise — printed warning on fallback.

## Guardrails

- Do not fan out a goroutine per Put — collector requests are already
  per-incident; serial writes inside a request are fine.
- Never log secrets. Log endpoint + bucket only.

## Done signal

Flip `C-01` in [`../dependencies.md`](../dependencies.md) to `done`.
