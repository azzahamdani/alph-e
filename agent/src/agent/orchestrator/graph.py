"""LangGraph ``StateGraph`` over ``IncidentState``.

Wires the skeleton nodes with the routing decisions from
``docs/devops-agent-architecture.md`` lines 204–213. The graph is
intentionally tiny for MVP1: intake → investigator → planner (``type=none``
stub) → coordinator → END. Additional edges exist so routing tests can
traverse them without reworking the wiring when real nodes land.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent.orchestrator import nodes, routing
from agent.schemas import IncidentState

if TYPE_CHECKING:  # pragma: no cover — type-only import
    from langgraph.graph.state import CompiledStateGraph


def build_graph(*, checkpointer: Any | None = None) -> CompiledStateGraph:
    """Compile and return the orchestrator graph.

    ``checkpointer`` is optional so tests can compile without Postgres. Production
    wiring passes one from :func:`agent.orchestrator.checkpoint.postgres_checkpointer`.
    """
    from langgraph.graph import END, StateGraph

    graph: StateGraph = StateGraph(IncidentState)

    graph.add_node(routing.NODE_INTAKE, nodes.intake_node)
    graph.add_node(routing.NODE_INVESTIGATOR, nodes.investigator_node)
    graph.add_node(routing.NODE_COLLECTORS, nodes.collectors_node)
    graph.add_node(routing.NODE_PLANNER, nodes.planner_node)
    graph.add_node(routing.NODE_DEV, nodes.dev_node)
    graph.add_node(routing.NODE_VERIFIER, nodes.verifier_node)
    graph.add_node(routing.NODE_REVIEWER, nodes.reviewer_node)
    graph.add_node(routing.NODE_COORDINATOR, nodes.coordinator_node)

    graph.set_entry_point(routing.NODE_INTAKE)

    graph.add_edge(routing.NODE_INTAKE, routing.NODE_INVESTIGATOR)

    # Investigator: loop via collectors, punt to planner, or escalate.
    graph.add_conditional_edges(
        routing.NODE_INVESTIGATOR,
        routing.route_after_investigator,
        {
            routing.NODE_COLLECTORS: routing.NODE_COLLECTORS,
            routing.NODE_PLANNER: routing.NODE_PLANNER,
            routing.NODE_COORDINATOR: routing.NODE_COORDINATOR,
        },
    )
    graph.add_edge(routing.NODE_COLLECTORS, routing.NODE_INVESTIGATOR)

    # Planner skeleton always returns type=none so we short-circuit to Coordinator.
    # Real routing (PR → Dev) will replace this edge once Planner reasoning lands.
    graph.add_edge(routing.NODE_PLANNER, routing.NODE_COORDINATOR)

    # Dev/Verifier/Reviewer cluster — edges present so routing tests exercise them.
    graph.add_edge(routing.NODE_DEV, routing.NODE_VERIFIER)
    graph.add_edge(routing.NODE_VERIFIER, routing.NODE_REVIEWER)
    graph.add_edge(routing.NODE_REVIEWER, routing.NODE_COORDINATOR)

    graph.add_edge(routing.NODE_COORDINATOR, END)

    compile_kwargs: dict[str, Any] = {}
    if checkpointer is not None:
        compile_kwargs["checkpointer"] = checkpointer
    return graph.compile(**compile_kwargs)
