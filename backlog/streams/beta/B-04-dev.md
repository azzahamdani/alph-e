---
id: B-04
subject: Dev agent — produces FixProposal with real diff
track: beta
depends_on: [F-01, F-02, F-03]
advances_wi: [WI-011]
---

## Goal

Replace the skeleton `dev_node`: read the signed `RemediationPlan` (`type=pr`)
and produce a `FixProposal` with one `FileChange` per modified file, valid
unified diffs, a Conventional-Commits message, and a PR body.

## Requirements

- LLM-driven via F-01/F-02/F-03.
- The agent receives:
  - The plan + the cited findings (slice projection from `IncidentState`).
  - A read-only repo snapshot — for MVP1 this is the local git working tree
    of the `target_repos` listed on the plan.
- Output Pydantic model: `DevOutput` with `proposal: FixProposal`,
  `reasoning: str`. Validate diffs syntactically (`patch --dry-run`-equivalent
  via `unidiff`) before returning.
- If the model cannot produce a valid diff after one corrective retry, return
  a `BlockedReport` instead of a partial proposal. Do not fall back to a
  pseudo-fix.

## Deliverables

- Replace `agent/src/agent/orchestrator/nodes/dev.py`.
- Add `DevOutput` to `nodes/_models.py`.
- New: `agent/src/agent/orchestrator/dev/diff_validator.py` (uses `unidiff`).
- Tests covering: diff validity gate, `BlockedReport` fallback, target-repo
  scoping, commit-message format check.

## Acceptance

- `mypy --strict` clean; tests pass without network.
- A test fixture with a known small repo and a known root cause produces a
  diff that applies cleanly via `git apply --check`.

## Guardrails

- Never write to disk. The Dev agent emits a diff; applying it is the
  human's call.
- Never modify files outside `plan.target_repos`.
- The PR body **must** reference the confirmed `Hypothesis.id` — Reviewer
  enforces this; failing here keeps the loop tight.

## Done signal

Flip `B-04` in [`../dependencies.md`](../dependencies.md) to `done`.
