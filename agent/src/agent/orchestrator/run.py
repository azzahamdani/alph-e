"""High-level graph runner — thin wrapper used by the intake API and tests.

``run_once`` compiles the graph with the supplied checkpointer and drives it
to completion for one incident.  A second call with the same ``incident_id``
and checkpointer will find the prior checkpoint at END and return immediately
with the persisted state — no nodes re-execute.
"""

from __future__ import annotations

from typing import Any

from agent.orchestrator.graph import build_graph
from agent.schemas import IncidentState


async def run_once(
    state: IncidentState,
    *,
    checkpointer: Any,
) -> IncidentState:
    """Run the graph to completion for *state*, respecting prior checkpoints.

    Args:
        state: Seed ``IncidentState``.  Must have a stable ``incident_id`` —
            this becomes the LangGraph ``thread_id`` so the checkpointer can
            correlate runs.
        checkpointer: An already-opened LangGraph saver (e.g. from
            :func:`agent.orchestrator.checkpoint.postgres_checkpointer`).

    Returns:
        The final ``IncidentState`` after the graph reaches END (or the last
        persisted state when a prior run already finished).
    """
    # Cast to Any so mypy doesn't resolve overloads on the langgraph Pregel type
    # (langgraph has ignore_missing_imports, but the installed stubs are visible).
    graph: Any = build_graph(checkpointer=checkpointer)
    config: dict[str, Any] = {"configurable": {"thread_id": state.incident_id}}

    # Check whether this thread already ran to completion.
    prior: dict[str, Any] | None = await checkpointer.aget(config)
    if prior is not None:
        # A checkpoint exists — return the persisted state without re-running.
        return _coerce(prior["channel_values"])

    # No prior checkpoint: run the graph from the seed state.
    # Consume the stream for side-effects (checkpoint writes after each node).
    async for _chunk in graph.astream(state.model_dump(), config=config):
        pass

    # After streaming, read back the final checkpoint to get merged state.
    final_cp: dict[str, Any] | None = await checkpointer.aget(config)
    if final_cp is None:
        raise RuntimeError(
            f"Checkpointer returned no state after graph run for incident_id={state.incident_id!r}. "
            "This is unexpected — the graph may have raised."
        )
    return _coerce(final_cp["channel_values"])


def _coerce(channel_values: dict[str, Any]) -> IncidentState:
    """Reconstruct an ``IncidentState`` from raw LangGraph channel values."""
    # LangGraph stores each state key as a separate channel; we re-merge them.
    return IncidentState.model_validate(channel_values)
