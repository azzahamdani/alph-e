# Build Progress — MVP1

This file is the team-facing companion to `backlog/streams/dependencies.md`.
Read it at the start of any new Claude Code session to understand where the
build is and what to do next.

## Single source of truth for task status

`backlog/streams/dependencies.md` — the dependency gate table. Update it
whenever you start or finish a task. Legend: `todo` / `in_progress` / `done` / `blocked`.

## How to continue the build

1. Open `backlog/streams/dependencies.md`.
2. Find the first tasks whose status is `todo` AND whose "Blocked by" deps are all `done`.
3. Dispatch the appropriate specialist agent for each unblocked task **in parallel** (one Agent tool call per task, all in the same message).
4. When each specialist completes, verify it flipped its row to `done`.
5. Repeat until X-01 is `done`.

### Specialist → Task mapping

| Task | Specialist type | Stream file |
|---|---|---|
| F-01 | `agent-builder` | `backlog/streams/alpha/F-01-llm-client.md` |
| F-02 | `agent-builder` | `backlog/streams/alpha/F-02-prompt-loader.md` |
| F-03 | `agent-builder` | `backlog/streams/alpha/F-03-structured-output.md` |
| F-04 | `agent-builder` | `backlog/streams/alpha/F-04-llm-observability.md` |
| F-05 | `agent-builder` | `backlog/streams/alpha/F-05-action-intent-signing.md` |
| F-06 | `evidence-specialist` | `backlog/streams/alpha/F-06-evidence-minio.md` |
| F-07 | `agent-builder` | `backlog/streams/alpha/F-07-postgres-checkpoint.md` |
| B-01 | `agent-builder` | `backlog/streams/beta/B-01-investigator.md` |
| B-02 | `agent-builder` | `backlog/streams/beta/B-02-collectors-dispatch.md` |
| B-03 | `agent-builder` | `backlog/streams/beta/B-03-planner.md` |
| B-04 | `agent-builder` | `backlog/streams/beta/B-04-dev.md` |
| B-05 | `agent-builder` | `backlog/streams/beta/B-05-verifier.md` |
| B-06 | `agent-builder` | `backlog/streams/beta/B-06-reviewer.md` |
| B-07 | `agent-builder` | `backlog/streams/beta/B-07-coordinator.md` |
| C-01 | `collector-specialist` | `backlog/streams/gamma/C-01-minio-writer.md` |
| C-02 | `collector-specialist` | `backlog/streams/gamma/C-02-prom-real.md` |
| C-03 | `collector-specialist` | `backlog/streams/gamma/C-03-loki-real.md` |
| C-04 | `collector-specialist` | `backlog/streams/gamma/C-04-kube-real.md` |
| X-01 | `eval-specialist` | `backlog/streams/` (cross-track) |

---

## Wave execution plan

Tasks with no unmet dependencies run in parallel within each wave.

### Wave 1 (no deps — run all in parallel)
F-01, F-02, F-05, F-06, C-01

### Wave 2 (after Wave 1 completes)
F-03 (needs F-01), F-04 (needs F-01), C-02 (needs C-01), C-03 (needs C-01), C-04 (needs C-01)

### Wave 3 (after Wave 2 completes)
F-07, B-01, B-02, B-03, B-04, B-05, B-06 — all in parallel

### Wave 4 (after B-03 + F-05 + F-06 done)
B-07

### Wave 5 (all previous done)
X-01 — end-to-end integration test

---

## Validation commands

```bash
# Python agent
task agent:lint        # ruff + mypy strict
task agent:test        # unit tests
task agent:test -- -m integration   # needs agent-infra running

# Go collectors
task collectors:lint   # golangci-lint
task collectors:test   # go test ./...
task collectors:build  # sanity-check all three binaries compile

# Infrastructure (must be up for integration tests)
task agent-infra:install
task agent-infra:postgres   # port-forward localhost:5432
task agent-infra:minio      # port-forward localhost:9000
```

---

## Current status (as of 2026-04-21)

**All 21 stream tasks are `done`. MVP1 build is complete.**

### What was built

**Alpha — Python plumbing (F-01 through F-07):**
- `agent/src/agent/llm/` — Anthropic client with prompt caching, structured output helper, observability
- `agent/src/agent/prompts/` — Prompt loader with role-specific system prompts
- `agent/src/agent/security/action_intent.py` — Ed25519 signer/verifier for ActionIntents
- `agent/src/agent/evidence/client.py` — Real MinIO + Postgres evidence client
- `agent/tests/integration/test_checkpoint.py` — Postgres checkpointer integration test

**Beta — reasoning nodes (B-01 through B-07):**
- `agent/src/agent/orchestrator/nodes/investigator.py` — LLM-driven hypothesis loop
- `agent/src/agent/orchestrator/nodes/collectors.py` — HTTP dispatch to Go collectors with cache
- `agent/src/agent/orchestrator/nodes/planner.py` — RemediationPlan + signed ActionIntent
- `agent/src/agent/orchestrator/nodes/dev.py` — FixProposal with real diff via unidiff
- `agent/src/agent/orchestrator/nodes/verifier.py` — git apply + kubectl dry-run checks
- `agent/src/agent/orchestrator/nodes/reviewer.py` — PR policy gate (hard rules + LLM soft judgement)
- `agent/src/agent/orchestrator/nodes/coordinator.py` — Deterministic lifecycle: verify → preflight → exec → escalate

**Gamma — Go collectors (C-01 through C-04):**
- `collectors/internal/evidence/minio_writer.go` — MinIO-backed evidence writer
- `collectors/internal/prom/` — Real PromQL dispatch
- `collectors/internal/loki/` — Real LogQL dispatch
- `collectors/internal/kube/` — Real client-go dispatch (read-only)

**Cross-track (X-01):**
- `agent/tests/integration/test_e2e_oom_alert.py` — End-to-end: OOM alert → Investigator → Planner → Coordinator (mocked LLM, no live deps)

### What's next (post-MVP1)

1. **Live in-cluster run** (default as of 2026-04-22): `export ANTHROPIC_API_KEY=... && task up` deploys cluster + monitoring + agent-infra + demo + collectors + agent. Port-forward the intake with `task agent:forward` and either wait for `PodOOMKilled` to fire or `task agent:fire` the fixture directly.
2. **Integration tests with real infra**: `task agent:test -- -m integration` (needs `task agent-infra:postgres` + `task agent-infra:minio` port-forwards in extra terminals).
3. **Haiku for Intake/Coordinator**: swap `claude-sonnet-4-6` to `claude-haiku-4-5-20251001` for faster/cheaper routing nodes.
4. **PR submission**: Coordinator's `type=pr` path triggers Dev → Verifier → Reviewer → real GitHub PR.
