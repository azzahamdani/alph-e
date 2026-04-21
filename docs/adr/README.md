# Architecture Decision Records

Load-bearing decisions about this project, referenced by ID from:
- Specialist agent prompts (`.claude/agents/*.md`)
- WorkItems in `backlog/`
- The architecture docs (`docs/devops-agent-architecture.md`, `docs/devops-agent-build-fleet.md`)

## Convention

- One file per decision: `NNNN-kebab-case-title.md`.
- Numbering is monotonic; never reuse an ID.
- Status lifecycle: `proposed` → `accepted` → (`superseded-by NNNN` | `deprecated`).
- When superseding an ADR, link both directions (the new one gets `Supersedes: NNNN`, the old one gets `Superseded-by: NNNN`).
- Keep the body tight: **Context**, **Decision**, **Consequences**. Rationale belongs in Context; future-looking caveats belong in Consequences.

## Index

| ID | Title | Status |
|---|---|---|
| [0001](0001-language-split-python-go.md) | Language split: Python orchestrator, Go collectors | accepted |
| [0002](0002-python-toolchain-uv-ruff-pytest-mypy.md) | Python toolchain: uv + ruff + pytest + mypy --strict | accepted |
| [0003](0003-go-toolchain-gomod-golangcilint-testify.md) | Go toolchain: Go modules + golangci-lint + testify | accepted |
| [0004](0004-runtime-framework-langgraph.md) | Runtime orchestrator framework: LangGraph | accepted |
| [0005](0005-intake-entry-alertmanager-webhook.md) | Intake entry point: Alertmanager webhook | accepted |
| [0006](0006-evidence-store-minio-postgres.md) | Evidence store: MinIO blobs + Postgres metadata | accepted |
| [0007](0007-build-fleet-claude-code-subagents.md) | Build fleet option: Claude Code subagents | accepted |
