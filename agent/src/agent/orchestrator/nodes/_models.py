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
