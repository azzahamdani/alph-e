# ADR-0008: Agent infrastructure in-cluster migration

- Status: accepted
- Date: 2026-04-21
- Supersedes: ADR-0006

## Context

ADR-0006 established Postgres + MinIO as the evidence store backend, deployed via host-side docker-compose. This made sense when the agent was purely host-side — one `docker-compose up` brought up the entire agent-side infrastructure.

The agent is moving in-cluster for MVP2 (better resource management, closer to production topology, easier scaling). Having agent dependencies on the host while the orchestrator runs in-cluster creates an integration seam: port-forwards, network configuration, credential management. Moving `agent-infra` in-cluster now, ahead of the agent itself, de-risks the larger move.

Additionally, the docker-compose approach doesn't reflect production patterns where storage layers run as managed services or in-cluster operators. Moving to Helm-managed deployments in the k3d cluster creates a more realistic substrate for testing.

## Decision

Replace the host-side docker-compose stack with in-cluster Helm deployments:

- **Postgres**: Bitnami postgresql chart with pgvector image override (`pgvector/pgvector:pg16`). 4Gi PVC on local-path storage class. EmptyDir for `/var/run/postgresql` to handle socket permissions. `allowInsecureImages` flag set to accept the pgvector image.

- **MinIO**: Official minio/minio chart (switched from Bitnami due to filesystem layout assumptions). Standalone mode with 10Gi PVC on local-path. Creates the `incidents` bucket natively via the chart's `buckets:` value. 30-day lifecycle rule applied via post-install Job at `agent-infra/manifests/minio-lifecycle-job.yaml`.

- **Deployment target**: `agent-infra` namespace in the existing k3d cluster.

- **Access model**:
  - Host-side dev loop: agent reaches dependencies via `task agent-infra:postgres` (localhost:5432) and `task agent-infra:minio` (localhost:9000 API + localhost:9001 console) port-forwards.
  - In-cluster (the default as of 2026-04-22): agent + collectors reach dependencies via internal service DNS (`postgres.agent-infra.svc.cluster.local:5432`, `minio.agent-infra.svc.cluster.local:9000`) through env vars in the `agent-secrets` Secret.

- **Credentials preserved**: devops/devops for Postgres, minio/minio-dev-secret for MinIO. Same bucket name (`incidents`), same TTL (30 days). EvidenceClient code unchanged during cutover.

## Consequences

**Positive:**
- Single bring-up command: `task up` handles cluster + monitoring + agent-infra + demo.
- Production-realistic deployment pattern: Helm, PVCs, service DNS, lifecycle rules.
- Removes docker-compose as a moving part — one fewer tool in the critical path.
- Agent move to in-cluster (MVP2) simplified — dependencies already in the right place.
- `task cluster:down` preserves agent state via PVCs (unless `task cluster:nuke`).

**Negative:**
- `task cluster:nuke` now wipes agent state (Postgres data + MinIO objects). Previously, docker volumes persisted across cluster restarts.
- Access commands change shape: `task agent-infra:psql` replaces `docker exec postgres psql`, `task agent-infra:logs` replaces `docker logs`.
- MVP1 requires port-forwards in extra terminals for direct database/object access during development.
- K3d cluster failure now takes down agent storage. Previously, compose could run independently.

**Migration path:**
Interface preserved across the cutover — same ports, same credentials, same bucket, same TTL policy, same pgvector extension. EvidenceClient, schema migrations, and lifecycle enforcement code didn't require changes. Test fixtures and development workflows remain valid.

**Follow-on (2026-04-22):** the agent orchestrator and Go collectors now also run in-cluster, in the `agent` namespace. See `agent/Dockerfile`, `collectors/Dockerfile`, `agent/manifests.yaml`, `collectors/manifests.yaml`, and the `agent-secrets` Secret seeded by `task agent:secret`. The Alertmanager webhook URL moved from `http://host.k3d.internal:8000/...` to `http://agent.agent.svc.cluster.local:8000/webhook/alertmanager` ([ADR-0005](0005-intake-entry-alertmanager-webhook.md)).