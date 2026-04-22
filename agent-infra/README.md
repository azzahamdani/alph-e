# agent-infra

In-cluster durable dependencies for the agent runtime:

- **Postgres (pgvector/pg16)** — LangGraph checkpoints, evidence metadata, semantic memory
- **MinIO** — S3-compatible evidence blob store, 30-day lifecycle
- **minio-bucket-bootstrap** — one-shot Job that creates the `incidents` bucket with the lifecycle rule

Installed via Helm. Storage uses k3d's default `local-path` StorageClass — PVCs bind to node-local directories. Data survives pod restarts and `task cluster:down`, but **not** `task cluster:nuke`.

Replaces the old host-side `infra/docker-compose.yaml`. See the successor ADR for context.

## Access

**In-cluster agent (the default — MVP2, live):** the agent + collectors read
their connection settings from the `agent-secrets` Secret in the `agent`
namespace, which defaults to internal service DNS:

```
POSTGRES_URL=postgresql://devops:devops@postgres.agent-infra.svc.cluster.local:5432/incidents
EVIDENCE_S3_ENDPOINT=http://minio.agent-infra.svc.cluster.local:9000
```

Seed or refresh the Secret with `task agent:secret`; override any field via
env vars in your shell before running it.

**Host-side dev loop:** port-forward each dependency to its familiar
localhost address — matches what `EvidenceClient` uses by default:

```
task agent-infra:postgres    # → localhost:5432
task agent-infra:minio       # → localhost:9000 (API) + localhost:9001 (console)
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
│   └── minio.values.yaml        # official minio/minio chart, standalone mode
└── manifests/
    └── minio-lifecycle-job.yaml   # one-shot: apply the 30-day lifecycle rule
```
