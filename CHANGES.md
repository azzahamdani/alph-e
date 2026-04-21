# agent-infra migration — what's in this PR and what's left for you

## What I changed

- **Added `agent-infra/`** — Helm values for Postgres (pgvector) and MinIO, plus a bucket bootstrap Job that replaces the old `minio-init` compose service.
- **Added `agent-infra:*` tasks** to `Taskfile.yml` — install, uninstall, nuke, status, port-forwards, psql shortcut.
- **Brought back the UI port-forward tasks** (`grafana`, `prometheus`, `alertmanager`) that had disappeared from `Taskfile.yml`.
- **Removed `infra/`** — the old docker-compose stack. Never got run in practice, so nothing to actually tear down.
- **Updated `README.md`** — the agent-side quick start now references `agent-infra/` and the port-forward workflow.
- **Updated `CLAUDE.md`** — three lines changed (layout summary, commands section, layout list).

## Interface preserved

The new in-cluster services listen on the same ports your agent code already targets:

| Service | Old (docker-compose) | New (agent-infra, via port-forward) |
|---|---|---|
| Postgres | `localhost:5432` | `localhost:5432` |
| MinIO S3 | `localhost:9000` | `localhost:9000` |
| MinIO Console | `localhost:9001` | `localhost:9001` |
| Postgres user/pass | `devops / devops` | `devops / devops` |
| Postgres db | `incidents` | `incidents` |
| MinIO root | `minio / minio-dev-secret` | `minio / minio-dev-secret` |
| Bucket | `incidents` | `incidents` |
| Lifecycle | 30 days | 30 days |
| pgvector | enabled at init | enabled at init |

**No `EvidenceClient` code changes required** while the agent is still host-side. Run `task agent-infra:postgres` and `task agent-infra:minio` in two terminals; everything Just Works as before.

When the agent moves in-cluster (your upcoming work), switch settings to:

```
POSTGRES_URL=postgres://devops:devops@postgres.agent-infra.svc.cluster.local:5432/incidents
EVIDENCE_S3_ENDPOINT=http://minio.agent-infra.svc.cluster.local:9000
```

## What I did NOT touch — your call

Several files still reference `infra/`. I deliberately left them alone because they're your build-fleet machinery:

### `.claude/agents/*.md`

- `infra-specialist.md` — has `allowed paths: infra/**`. Either retire this specialist (no more `infra/`) or rename it to `agent-infra-specialist` with new paths (`agent-infra/**` + the `agent-infra:*` Taskfile block).
- `tech-lead.md` — lists `infra/` among blocked paths ("you don't write code in these"). Needs `agent-infra/` in its place.
- `agent-builder.md`, `collector-specialist.md`, `docs-specialist.md`, `eval-specialist.md` — all have `infra/**` in their blocked paths. Same swap to `agent-infra/**`.

### `backlog/`

- `WI-002-infra-docker-compose.yaml` — work item that created the original docker-compose stack. Mark complete/superseded, or delete.
- `WI-015-docs-update.yaml` — references the old layout. Worth updating the acceptance criteria.

### `docs/devops-agent-build-fleet.md`

Line 178 has a table row referring to the `Infra` specialist's tools. Same rename call as the specialist file.

### ADRs

- **ADR-0006** (evidence store) — you said you'd rewrite it. The pending ADR (call it ADR-0008) should supersede 0006 and document: in-cluster + local-path + Helm + same interface.
- **ADR-0005** (intake webhook URL) — the `host.k3d.internal` URL is still correct *now* (agent host-side). Add a note that it changes when the agent moves in-cluster.

## Suggested commit sequence

1. **This PR** — new `agent-infra/` + Taskfile + docs + deletion of `infra/` (this work, all in one commit since the old stack was never running).
2. **Your ADR rewrite** — 0008 supersedes 0006, amends 0005.
3. **Build-fleet updates** — rename/retire specialists, update `tech-lead.md` scope, update blocked paths. Same commit as the ADR, probably.
4. **Backlog cleanup** — mark WI-002 complete, update WI-015.

Steps 2–4 are yours; 1 is me.

## Testing this before merge

```bash
# 1. Bring up the cluster if it isn't already
task cluster:up

# 2. Install the new in-cluster stack
task agent-infra:install

# 3. Verify the bucket job succeeded
kubectl -n agent-infra logs job/minio-bucket-bootstrap
# Should end with "✓ Bucket bootstrap complete"

# 4. Verify pgvector is available
task agent-infra:psql
# \dx
# (should show 'vector' extension in the list)

# 5. Verify MinIO bucket + lifecycle
task agent-infra:minio      # in another terminal
# Open http://localhost:9001 → login: minio / minio-dev-secret
# Confirm 'incidents' bucket exists and has a lifecycle rule

# 6. Run your existing agent test suite against the new stack
task agent:test             # should pass unchanged
```

## Rollback

If something breaks on your side:

```bash
task agent-infra:nuke       # blow away the in-cluster stack
git revert HEAD             # restore the old infra/ directory
task infra:up               # bring docker-compose back (the old path)
```

The old docker-compose stack was never running in the main development flow here, but the files are preserved in git history if you need to revert.
