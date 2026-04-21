"""Linear intake — placeholder for post-MVP1.

The shape mirrors ``webhook.alertmanager_webhook`` so the orchestrator hand-off
is identical regardless of where an incident originated.
"""

from __future__ import annotations

from agent.schemas import IncidentState


def seed_from_linear_issue(issue_id: str) -> IncidentState:
    """Fetch a Linear issue and seed an ``IncidentState`` from it.

    Not implemented for MVP1. When implemented, use the webhook intake pattern
    and preserve Linear's issue id in ``alert.labels['linear_issue_id']``.
    """
    raise NotImplementedError(
        f"Linear intake is deferred past MVP1. Tried to seed from {issue_id!r}."
    )
