# ADR-0007: Build fleet option — Claude Code subagents

- Status: accepted
- Date: 2026-04-21

## Context

`docs/devops-agent-build-fleet.md` presents three implementation options for the construction-side fleet:

1. Claude Code Task-tool subagents (simplest, ≤10 concurrent).
2. Archon Agent Work Orders (full-circle use of a repo designed for this).
3. Custom LangGraph/PydanticAI orchestrator eating its own dogfood.

MVP1 has maybe 15–20 WorkItems total, partial-parallelism within phases. Option 1 hits the sweet spot: no new orchestration infra, subagent contexts are genuinely isolated, path scoping is enforceable via the agent definition's allowed tools.

## Decision

Use **Claude Code subagents** via `.claude/agents/*.md`.

- One agent definition per specialist role from the build-fleet tool-surface table (`docs/devops-agent-build-fleet.md:172-187`):
  `tech-lead`, `schema-specialist`, `infra-specialist`, `evidence-specialist`, `collector-specialist`, `agent-builder`, `integrations-specialist`, `eval-specialist`, `docs-specialist`, `reviewer`.
- Each agent's tool list is narrow per the table. Example: `schema-specialist` gets Read/Edit/Write under `agent/src/agent/schemas/`, `mypy`, `ruff`, `pytest`; nothing else.
- Tech Lead dispatches via the standard Agent tool, passing a serialised WorkItem from `backlog/WI-NNN-*.yaml` as the agent's prompt seed.
- Reviewer runs `task agent:lint`, `task agent:test`, `task collectors:lint`, `task collectors:test` as appropriate per the PR's touched paths.

Post-MVP1 escape hatch: if the fleet grows past ~15 concurrent specialists, reconsider Option 3.

## Consequences

- **+** Zero orchestration infra — Claude Code already does subagent dispatch, isolated contexts, tool scoping.
- **+** The `Agent` tool's `isolation: worktree` mode gives us parallel work on branches without stepping on each other's file state.
- **+** Mirrors the runtime architecture (narrow ephemeral specialists + stateful orchestrator). The patterns compound.
- **−** Tech Lead state isn't truly persistent across sessions — `BuildState` lives in conversation memory. For MVP1 scale this is fine; the `backlog/` YAML and git are the durable source of truth.
- **−** Claude Code subagents don't have a native merge-queue or CI-interlock. The Reviewer agent's approval is advisory; merge still goes through GitHub (same discipline as the build-fleet doc's "Reviewer approval is local, not sufficient for merge").
- Migration to Option 3 is possible but non-trivial: the subagent definitions become LangGraph nodes; the Tech Lead gets its own StateGraph over BuildState. Not for MVP1.
