"""Per-node Pydantic output models.

Every reasoning node uses ``complete_typed`` which requires a concrete Pydantic
model.  Models live here — not inside the node modules — so Beta tasks can
share them without circular imports.

Only add models for nodes that have landed real LLM reasoning.  Skeleton nodes
return plain ``dict[str, object]`` and do not need an entry here.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from agent.schemas.incident import Hypothesis
from agent.schemas.remediation import FileChange, FixProposal


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
