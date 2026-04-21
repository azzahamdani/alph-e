"""Coordinator sub-modules: preflight, exec, and escalation."""

from agent.orchestrator.coordinator.escalation import build_escalation_package
from agent.orchestrator.coordinator.exec import ExecutionRecord, IdempotentExecutor
from agent.orchestrator.coordinator.preflight import (
    PreflightOutcome,
    PreflightResult,
    check_preflight,
)

__all__ = [
    "ExecutionRecord",
    "IdempotentExecutor",
    "PreflightOutcome",
    "PreflightResult",
    "build_escalation_package",
    "check_preflight",
]
