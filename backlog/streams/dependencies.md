# Dependency gate — single source of truth for stream status

Agents **must** update this table before and after touching code. See the
protocol in [`README.md`](README.md).

Legend: `todo` → nothing started. `in_progress` → owner has taken the row.
`done` → lint + tests green, committed. `blocked` → waiting on an issue
described in the Notes column.

## Alpha — shared Python plumbing

| ID | Subject | Status | Owner | Blocked by | Notes |
|---|---|---|---|---|---|
| F-01 | Anthropic LLM client + prompt caching | done | agent-builder | — | |
| F-02 | Prompt loader with system.md prefix | done | agent-builder | — | |
| F-03 | Structured-output helper (Pydantic + retry) | done | agent-builder | F-01 | |
| F-04 | LLM-call observability (structlog + cost) | done | agent-builder | F-01 | |
| F-05 | ActionIntent signer / verifier | done | agent-builder | — | |
| F-06 | Evidence client — MinIO + Postgres impl | done | evidence-specialist | — | |
| F-07 | Postgres checkpointer integration test | done | agent-builder | F-06 | |

## Beta — reasoning nodes

| ID | Subject | Status | Owner | Blocked by | Notes |
|---|---|---|---|---|---|
| B-01 | Investigator node (real LLM) | done | agent-builder | F-01, F-02, F-03, F-04 | |
| B-02 | Collectors dispatch (HTTP client + cache) | done | agent-builder | F-04, F-06 | |
| B-03 | Planner node (RemediationPlan + ActionIntent) | done | agent-builder | F-01, F-02, F-03, F-05 | |
| B-04 | Dev agent (FixProposal with real diff) | done | agent-builder | F-01, F-02, F-03 | |
| B-05 | Verifier node (dry-run checks) | done | agent-builder | F-01, F-02, F-03 | |
| B-06 | Reviewer node (PR policy gate) | done | agent-builder | F-01, F-02, F-03 | |
| B-07 | Coordinator node (exec + escalation) | in_progress | agent-builder | — | F-05, F-06, B-03 | |

## Gamma — Go collectors

| ID | Subject | Status | Owner | Blocked by | Notes |
|---|---|---|---|---|---|
| C-01 | MinIO-backed evidence Writer | done | collector-specialist | — | |
| C-02 | prom-collector real PromQL dispatch | done | collector-specialist | C-01 | |
| C-03 | loki-collector real LogQL dispatch | done | collector-specialist | C-01 | |
| C-04 | kube-collector real client-go dispatch | done | collector-specialist | C-01 | |

## Cross-track integration

| ID | Subject | Status | Owner | Blocked by | Notes |
|---|---|---|---|---|---|
| X-01 | End-to-end happy path: OOM alert → escalated | todo | — | B-01, B-02, B-03, B-07, C-02, C-03, C-04 | Exercises the full graph with real agents and real collectors against the lab cluster. |
