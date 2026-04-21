# ADR-0004: Runtime orchestrator framework — LangGraph

- Status: accepted
- Date: 2026-04-21

## Context

The architecture doc's runtime is an explicit graph: Intake → Investigator ↔ Collectors → Planner → {Dev → Verifier → Coordinator | Coordinator directly | Escalation} with routing rules spelled out in `docs/devops-agent-architecture.md:202-213`. The system needs:

- First-class **typed state** flowing through nodes (IncidentState + refined slices per node).
- **Explicit edges** with conditional routing based on `VerifierResult.kind`, `RemediationPlan.type`, reviewer decision, etc. These aren't prompt-level decisions — they're graph-level.
- **Checkpointing** after every step so the orchestrator can resume after crashes, deploys, or long pauses (arch doc's memory architecture makes this load-bearing).
- **Subagent dispatch** where collectors run with isolated contexts and return only refined outputs.

Two finalist frameworks from the arch doc's "Suggested stack":

- **LangGraph**: explicit graph nodes and edges; first-class checkpointers (Postgres, SQLite, memory); subgraph dispatch; mature async; maps 1:1 to the mermaid diagrams in `docs/diagrams/system-architecture.svg`.
- **PydanticAI**: lighter, typed-state-first, less ceremony. Orchestration is "just functions over state." Weaker checkpointing story; graph visualisation is implicit.

## Decision

Adopt **LangGraph** for MVP1.

Specifics:
- `StateGraph` over `IncidentState` (Pydantic v2 model).
- Nodes live in `agent/src/agent/orchestrator/nodes/` — one file per box in the system-architecture diagram.
- Routing rules live in `agent/src/agent/orchestrator/routing.py` and are a direct transcription of the table at `docs/devops-agent-architecture.md:202-213`.
- Checkpointer: `PostgresSaver` from `langgraph-checkpoint-postgres`, wired via `agent/src/agent/orchestrator/checkpoint.py` against the Postgres instance in `infra/docker-compose.yaml`.
- Subagent dispatch: LangGraph's native `Send` + subgraph pattern for collectors.

PydanticAI stays on the table for the Dev agent specifically — it's the one place where "just a function over typed state" might fit better. Revisit post-MVP1 with baseline data.

## Consequences

- **+** The code maps 1:1 to the architecture diagrams. A reviewer can diff a PR against the diagram. This pays off heavily when specialists are ephemeral and don't retain architectural context.
- **+** `PostgresSaver` gives us resumability for free against the same Postgres instance that holds evidence metadata.
- **+** Subgraph dispatch means collectors are literal subgraphs with their own context — matches the arch doc's subagent token-management principle without extra machinery.
- **−** LangGraph is heavier than PydanticAI. Extra learning curve for specialists that touch the orchestrator layer.
- **−** LangGraph's typing with Pydantic has historically had rough edges. We'll budget for `# type: ignore` comments and track them.
- **−** If we later want the Dev agent to be "just a function," we'll likely mount a PydanticAI agent as a LangGraph node. That's a known pattern; not free but not expensive.
