"""LangGraph wiring for the DevOps investigation agent.

Every node takes a slice of ``IncidentState``, returns a slice-update, and the
graph merges. The only durable container is ``IncidentState`` — node-local
memory does not survive checkpoints.

Mermaid source of truth: ``docs/diagrams/system-architecture.mmd``.
Routing table: ``docs/devops-agent-architecture.md`` lines 204–213.
"""

from agent.orchestrator.graph import build_graph

__all__ = ["build_graph"]
