"""Graph nodes. Each module is one role per the architecture diagram.

All nodes are skeleton stubs for MVP1 — they perform no LLM calls and return
pass-through or trivially-typed outputs. Real reasoning is added per WorkItem
once the graph compiles cleanly end-to-end.
"""

from agent.orchestrator.nodes.collectors import collectors_node
from agent.orchestrator.nodes.coordinator import coordinator_node
from agent.orchestrator.nodes.dev import dev_node
from agent.orchestrator.nodes.intake import intake_node
from agent.orchestrator.nodes.investigator import investigator_node
from agent.orchestrator.nodes.planner import planner_node
from agent.orchestrator.nodes.reviewer import reviewer_node
from agent.orchestrator.nodes.verifier import verifier_node

__all__ = [
    "collectors_node",
    "coordinator_node",
    "dev_node",
    "intake_node",
    "investigator_node",
    "planner_node",
    "reviewer_node",
    "verifier_node",
]
