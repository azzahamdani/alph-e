# Host-side infra

Docker Compose stack that runs alongside (not inside) the k3d lab cluster.
Home for the agent's Postgres + MinIO dependencies.

## Services

| Service | Purpose | Host port |
|---|---|---|
| `postgres` (pgvector/pg16) | LangGraph checkpoints, evidence metadata, semantic memory | `5432` |
| `minio` | S3-compatible evidence blob store | `9000` (API), `9001` (console) |
| `minio-init` | One-shot: creates `incidents` bucket with a 30-day lifecycle rule | — |

## Usage

```
task infra:up       # docker compose up -d
task infra:logs     # tail logs
task infra:psql     # shell into Postgres
task infra:down     # compose down (keeps volumes)
```

## Defaults

Credentials live in env vars so they can be overridden without editing the compose file. Defaults (local-dev only):

```
POSTGRES_USER=devops
POSTGRES_PASSWORD=devops
MINIO_ROOT_USER=minio
MINIO_ROOT_PASSWORD=minio-dev-secret
```

Reference: [ADR-0006](../docs/adr/0006-evidence-store-minio-postgres.md).
