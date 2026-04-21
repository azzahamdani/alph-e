"""Collector dispatch sub-package.

Exposes the three public surfaces used by ``collectors_node``:

- :mod:`registry` — hypothesis-label → collector-name mapping.
- :mod:`cache`    — Postgres-backed 5-minute result cache.
- :mod:`http`     — ``httpx``-based async POST client.
"""

from agent.orchestrator.dispatch.cache import CollectorCache, make_cache_key
from agent.orchestrator.dispatch.http import dispatch
from agent.orchestrator.dispatch.registry import (
    COLLECTOR_KUBE,
    COLLECTOR_LOKI,
    COLLECTOR_PROM,
    select_collector,
)

__all__ = [
    "COLLECTOR_KUBE",
    "COLLECTOR_LOKI",
    "COLLECTOR_PROM",
    "CollectorCache",
    "dispatch",
    "make_cache_key",
    "select_collector",
]
