# Build streams — parallel tracks with a dependency gate

This directory is the execution plan for turning the agent skeleton into a
working MVP1. Work is sliced into three tracks that run **in parallel** and
coordinate via a single gate file: [`dependencies.md`](dependencies.md).

## Tracks

| Track | Owner role | Focus |
|---|---|---|
| **Alpha** | `agent-builder` + `evidence-specialist` | Shared Python plumbing — LLM client, prompt loader, structured outputs, ActionIntent signing, evidence store, checkpointer. |
| **Beta** | `agent-builder` | Reasoning nodes — Investigator, Collectors dispatch, Planner, Dev, Verifier, Reviewer, Coordinator. |
| **Gamma** | `collector-specialist` | Go collectors — MinIO writer + real Prom / Loki / kube dispatch. |

## Protocol — how dependencies are honoured

Every subtask in [`alpha/`](alpha/), [`beta/`](beta/), and [`gamma/`](gamma/) is
a single markdown file with a frontmatter block declaring its `depends_on` set
using the task IDs. Before an agent starts a subtask, it **must**:

1. Open [`dependencies.md`](dependencies.md).
2. For each ID in the subtask's `depends_on`, find the row. If its status is
   **not** `done`, stop and report "blocked on `<ID>`". Do not start the work.
3. If all `depends_on` are `done`, update this subtask's row to `in_progress`
   and commit that change before touching any code.
4. Do the work. Lint + tests green is the definition of "done".
5. Flip the row to `done` and commit. The next waiter may now proceed.

**Invariants:**

- Only one agent ever owns a given row at a time. Take ownership by setting
  the `Owner` column to your agent name when you move to `in_progress`.
- The gate file is the **only** source of truth for status. Do not assume a
  task is done because its file exists.
- If you discover new work mid-task, add a new row to the gate and a new
  file in the appropriate track directory — do not silently expand scope.
- Never mark a task `done` if tests or lint are failing. Leave it
  `in_progress` and create a new follow-up row describing the blocker.

## Task ID scheme

- `F-NN` — foundation (Alpha)
- `B-NN` — agent reasoning node (Beta)
- `C-NN` — Go collector (Gamma)

## Relationship to `WI-*`

The `WI-*.yaml` backlog in the parent directory remains the canonical, spec-level
work-item list. The streams break those WIs into gate-coordinated subtasks.
Each stream file references the WIs it advances.

## Launching the tracks

Kick each track off with its dedicated build-fleet subagent (see
[`.claude/agents/`](../../.claude/agents/)). The three runs can be launched in
the same message for genuine parallel execution — they will naturally serialise
where dependencies force it, via the gate file.
