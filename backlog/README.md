# Build-fleet backlog

Typed `WorkItem`s dispatched by the Tech Lead agent to specialists.

Schema is the `WorkItem` contract at `docs/devops-agent-build-fleet.md:47-63`:

```yaml
id: WI-NNN
title: <short imperative>
component: <dotted scope, e.g. schemas.incident, collectors.prom>
acceptance_criteria:
  - <testable, specific statement>
interface_contracts:
  - <relevant Pydantic models or MCP schemas the specialist must honour>
relevant_adrs:
  - <ADR IDs>
allowed_paths:
  - <glob>
blocked_paths:
  - <glob>
depends_on:
  - <other WorkItem IDs>
estimated_complexity: small | medium | large
max_iterations: <int>
```

## Dispatch rules

- Tech Lead picks the lowest-numbered ready item (all `depends_on` satisfied) and dispatches.
- Parallel dispatch is allowed only when WorkItems have disjoint `allowed_paths` AND share no `interface_contracts` they would both write.
- On specialist completion, Tech Lead marks the item `status: done` by renaming or moving the file into an `archive/` subdirectory (flat YAML keeps git diffs clean).

## Current phase

Phase 1 (Foundation) — schemas, infra, evidence store must land before collectors and agents.
