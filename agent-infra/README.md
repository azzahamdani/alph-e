# agent-infra

In-cluster durable dependencies for the agent runtime:

- **Postgres (pgvector/pg16)** — LangGraph checkpoints, evidence metadata, semantic memory
- **MinIO** — S3-compatible evidence blob store, 30-day lifecycle
- **minio-bucket-bootstrap** — one-shot Job that creates the `incidents` bucket with the lifecycle rule

Installed via Helm. Storage uses k3d's default `local-path` StorageClass — PVCs bind to node-local directories. Data survives pod restarts and `task cluster:down`, but **not** `task cluster:nuke`.

Replaces the old host-side `infra/docker-compose.yaml`. See the successor ADR for context.

## Access

**MVP1 — agent runs on your host:** port-forwards preserve the `localhost:5432` / `localhost:9000` interface the agent code already expects. No `EvidenceClient` changes needed.

```
task agent-infra:postgres    # → localhost:5432
task agent-infra:minio       # → localhost:9000 (API) + localhost:9001 (console)
```

**MVP2 — agent runs in-cluster:** internal service DNS. `EvidenceClient` settings switch to:

```
POSTGRES_URL=postgres://devops:devops@postgres.agent-infra.svc.cluster.local:5432/incidents
EVIDENCE_S3_ENDPOINT=http://minio.agent-infra.svc.cluster.local:9000
```

## Credentials (lab only)

| Service | User | Password |
|---|---|---|
| Postgres (app) | `devops` | `devops` |
| Postgres (superuser) | `postgres` | `devops-postgres` |
| MinIO root | `minio` | `minio-dev-secret` |

These match the old compose defaults so the migration is drop-in. Lab-only credentials — do not reuse anywhere else.

## Layout

```
agent-infra/
├── values/
│   ├── postgresql.values.yaml   # Bitnami chart, pgvector image override
│   └── minio.values.yaml        # Bitnami chart, standalone mode
└── manifests/
    └── bucket-bootstrap-job.yaml   # one-shot: mc mb + ilm rule
```
