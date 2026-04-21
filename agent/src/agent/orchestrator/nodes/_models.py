"""Per-node Pydantic output models.

Every reasoning node uses ``complete_typed`` which requires a concrete Pydantic
model.  Models live here — not inside the node modules — so Beta tasks can
share them without circular imports.

Only add models for nodes that have landed real LLM reasoning.  Skeleton nodes
return plain ``dict[str, object]`` and do not need an entry here.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from agent.schemas.incident import ActionIntent, Hypothesis
from agent.schemas.remediation import FileChange, FixProposal, RemediationPlan
from agent.schemas.verifier import VerifierResultKind


class VerifierLLMOutput(BaseModel):
    """Structured output produced by the Verifier node after interpreting dry-run results.

    The LLM fills this via forced tool-use so the kind and reasoning are
    Pydantic-validated before they touch ``VerifierResult``.
    """

    kind: VerifierResultKind = Field(
        ...,
        description=(
            "passed — all checks passed; route to Reviewer. "
            "implementation_error — patch is wrong but diagnosis is defensible; route to Dev. "
            "diagnosis_invalidated — dry-run evidence contradicts root-cause; route to Investigator. "
            "When uncertain, prefer diagnosis_invalidated over implementation_error."
        ),
    )
    reasoning: str = Field(
        ...,
        description=(
            "Concise explanation of which checks ran, what they returned, "
            "and why this kind was chosen.  Required — never empty."
        ),
    )
    failures: list[str] = Field(
        default_factory=list,
        description=(
            "Human-readable description of each failed check.  "
            "Empty when kind=passed."
        ),
    )


class ReviewDecision(StrEnum):
    """Possible decisions the Reviewer can emit."""

    approve = "approve"
    request_changes_on_fix = "request_changes_on_fix"
    challenge_root_cause = "challenge_root_cause"


class ReviewerOutput(BaseModel):
    """Structured output produced by the Reviewer node.

    The LLM fills this via forced tool-use; hard policy checks may override
    ``decision`` to ``request_changes_on_fix`` before the node returns.

    ``decision`` is typed as a ``Literal`` union of the allowed string values so
    that Pydantic strict-mode validation (used by ``complete_typed``) accepts the
    plain strings returned by the LLM tool-use block.  The ``ReviewDecision``
    StrEnum provides named constants for all other code that handles a
    ``ReviewerOutput`` value.
    """

    decision: Literal["approve", "request_changes_on_fix", "challenge_root_cause"] = Field(
        ...,
        description=(
            "approve — PR is ready for human merge. "
            "request_changes_on_fix — send back to Dev. "
            "challenge_root_cause — root cause is wrong; re-investigate."
        ),
    )
    reasoning: str = Field(
        ...,
        description=(
            "Explanation of the decision. If hard policy rules failed, cite them. "
            "If challenging root cause, explain which evidence contradicts the hypothesis."
        ),
    )
    cited_evidence_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Evidence IDs from findings that support the decision. "
            "Required (non-empty) when decision=challenge_root_cause."
        ),
    )


class InvestigatorOutput(BaseModel):
    """Structured output produced by the Investigator node each tick.

    The LLM fills this via forced tool-use so all output is Pydantic-validated
    before it touches ``IncidentState``.
    """

    hypotheses: list[Hypothesis] = Field(
        ...,
        description=(
            "Full updated hypothesis list.  Existing entries must be included with "
            "their original ids so merge logic can detect what changed."
        ),
    )
    current_focus_hypothesis_id: str = Field(
        ...,
        description=(
            "The id of the hypothesis whose question the Collectors node should "
            "answer next.  Must be present in the hypotheses list."
        ),
    )
    reasoning: str = Field(
        ...,
        description=(
            "One-paragraph explanation of why these hypotheses were proposed or "
            "updated and why the focus hypothesis was chosen."
        ),
    )


class PlannerOutput(BaseModel):
    """Structured output produced by the Planner node.

    The LLM fills this via forced tool-use.  Mutating plan types
    (``rollback``, ``scale``, ``flag_flip``) require a signed
    ``ActionIntent``; the Planner node enforces this invariant after
    the LLM call and before writing to ``IncidentState``.
    """

    plan: RemediationPlan = Field(
        ...,
        description=(
            "The chosen remediation plan.  ``type=none`` is a valid outcome. "
            "Confidence < 0.5 must produce ``type=none``."
        ),
    )
    intent: ActionIntent | None = Field(
        default=None,
        description=(
            "A signed ActionIntent for mutable plan types "
            "(rollback, scale, flag_flip).  Leave None for pr, runbook, none."
        ),
    )
    reasoning: str = Field(
        ...,
        description=(
            "One-paragraph explanation of the decision: which hypothesis drove "
            "the plan, why this type was chosen, and what evidence was used."
        ),
    )


class DevLLMOutput(BaseModel):
    """Raw structured output requested from the LLM by the Dev node.

    The LLM fills this via forced tool-use.  The Dev node validates the
    diffs syntactically (via ``unidiff``) before constructing the final
    ``FixProposal``; a validation failure triggers one corrective retry,
    after which the node returns a ``BlockedReport``.
    """

    changes: list[FileChange] = Field(
        ...,
        description=(
            "One FileChange per modified file.  ``repo`` must be one of the repos "
            "listed on the RemediationPlan.  ``diff`` must be a valid unified diff "
            "(--- a/path / +++ b/path header, then one or more hunks)."
        ),
    )
    commit_message: str = Field(
        ...,
        description=(
            "Conventional Commits format, imperative voice. "
            "Example: 'fix(leaky-service): cap background allocator to 1 MB/s'. "
            "Must not exceed 72 characters on the first line."
        ),
    )
    pr_body: str = Field(
        ...,
        description=(
            "Markdown PR body.  Must include: ## Summary, ## Root cause "
            "(citing the confirmed Hypothesis.id verbatim), ## Test plan. "
            "Reference the hypothesis id using the exact string from the plan context."
        ),
    )
    branch_name: str = Field(
        ...,
        description=(
            "Git branch name for this PR, e.g. 'fix/oom-cap-allocator-rate'. "
            "Lowercase, hyphens only, no spaces."
        ),
    )
    reasoning: str = Field(
        ...,
        description=(
            "One-paragraph explanation of what was changed and why — "
            "for human reviewers and the audit trail."
        ),
    )


class DevOutput(BaseModel):
    """What the Dev node returns to the orchestrator after a successful run.

    ``proposal`` is the fully-validated ``FixProposal``; ``reasoning`` is the
    Dev agent's explanation for the audit trail.
    """

    proposal: FixProposal = Field(
        ...,
        description="The validated FixProposal ready for the Verifier.",
    )
    reasoning: str = Field(
        ...,
        description="Dev agent's rationale; written to the timeline.",
    )
