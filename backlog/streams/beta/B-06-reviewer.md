---
id: B-06
subject: Reviewer node — PR policy gate
track: beta
depends_on: [F-01, F-02, F-03]
advances_wi: [WI-011]
---

## Goal

Replace the skeleton `reviewer_node` so it gates `FixProposal` outputs against
hard policy rules and a softer "challenge or accept" judgement.

## Requirements

- LLM-driven via F-01/F-02/F-03 for the soft judgement.
- Hard rules (Python, no LLM):
  - PR diff must touch only files within `plan.target_repos` — reject otherwise.
  - Commit message must contain the confirmed `Hypothesis.id` — reject otherwise.
  - PR body must reference at least one `Finding.evidence_id` — reject otherwise.
- Soft judgement: returns one of `approve`, `request_changes_on_fix`,
  `challenge_root_cause`. Default to `request_changes_on_fix` when ambiguous.
- Output Pydantic model: `ReviewerOutput` with `decision`, `reasoning`,
  `cited_evidence_ids: list[str]`.

## Deliverables

- Replace `agent/src/agent/orchestrator/nodes/reviewer.py`.
- New: `agent/src/agent/orchestrator/reviewer/policy.py` for the hard checks.
- Tests covering: each hard-rule rejection path; ambiguity defaulting to
  `request_changes_on_fix`; explicit-challenge routing to Investigator.

## Acceptance

- `mypy --strict` clean.
- Tests pass without network. The reviewer never emits `approve` when any
  hard check fails — that case is auto-`request_changes_on_fix` with the
  failing rule cited in `reasoning`.

## Guardrails

- Reviewer never edits the proposal. It approves, or routes back.
- The `challenge_root_cause` route must include cited evidence that
  contradicts the confirmed hypothesis — empty citations → fall back to
  `request_changes_on_fix`.

## Done signal

Flip `B-06` in [`../dependencies.md`](../dependencies.md) to `done`.
