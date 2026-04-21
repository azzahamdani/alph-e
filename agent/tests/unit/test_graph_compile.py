"""The orchestrator graph compiles without a checkpointer.

Keeping this minimal: a compile-only test catches wiring mistakes (missing
nodes, unreachable states, circular conditional edges) without requiring
Postgres. End-to-end traversal is in the integration suite.
"""

from __future__ import annotations

from agent.orchestrator import build_graph


def test_graph_compiles_without_checkpointer() -> None:
    compiled = build_graph()
    assert compiled is not None
    # The compiled graph exposes the node names we registered.
    graph = compiled.get_graph()
    expected = {
        "intake",
        "investigator",
        "collectors",
        "planner",
        "dev",
        "verifier",
        "reviewer",
        "coordinator",
    }
    assert expected.issubset(set(graph.nodes))
