"""Postgres-backed collector result cache.

Cache key components (from arch doc §Caching):

    (incident_id, collector_name, question, time_range,
     scope_services, environment_fingerprint)

TTL: 5 minutes. A miss returns ``None``; a hit returns the cached ``Finding``.
The write path (``put``) is called *after* a successful collector response —
never before (guardrail from B-02 spec).
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta

import psycopg

from agent.schemas.collector import (
    EnvironmentFingerprint,
    Finding,
    TimeRange,
)

logger = logging.getLogger(__name__)

_TTL = timedelta(minutes=5)

# ---------------------------------------------------------------------------
# Cache key
# ---------------------------------------------------------------------------


def make_cache_key(
    *,
    incident_id: str,
    collector_name: str,
    question: str,
    time_range: TimeRange,
    scope_services: list[str],
    environment_fingerprint: EnvironmentFingerprint,
) -> str:
    """Derive a stable, deterministic cache key as a hex digest.

    The key is SHA-256 over a canonical JSON representation of all components.
    Sorting list fields ensures key stability regardless of insertion order.
    """
    components: dict[str, object] = {
        "incident_id": incident_id,
        "collector_name": collector_name,
        "question": question,
        "time_range": {
            "start": time_range.start.isoformat(),
            "end": time_range.end.isoformat(),
        },
        "scope_services": sorted(scope_services),
        "environment_fingerprint": {
            "cluster": environment_fingerprint.cluster,
            "account": environment_fingerprint.account,
            "region": environment_fingerprint.region,
            "deploy_revision": environment_fingerprint.deploy_revision,
            "rollout_generation": environment_fingerprint.rollout_generation,
        },
    }
    raw = json.dumps(components, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class CollectorCache:
    """Thin async wrapper around the ``collector_cache`` Postgres table."""

    def __init__(self, postgres_url: str) -> None:
        self._postgres_url = postgres_url

    async def get(self, cache_key: str) -> Finding | None:
        """Return the cached ``Finding`` or ``None`` if absent/expired."""
        async with await psycopg.AsyncConnection.connect(self._postgres_url) as conn:
            row = await conn.execute(
                """
                SELECT finding_json
                FROM collector_cache
                WHERE cache_key = %s
                  AND expires_at > NOW()
                """,
                (cache_key,),
            )
            record = await row.fetchone()
        if record is None:
            logger.debug("cache miss", extra={"cache_key": cache_key[:16]})
            return None
        logger.debug("cache hit", extra={"cache_key": cache_key[:16]})
        return Finding.model_validate(record[0])

    async def put(
        self,
        *,
        cache_key: str,
        incident_id: str,
        collector_name: str,
        question: str,
        finding: Finding,
    ) -> None:
        """Persist a ``Finding`` to the cache.

        Called only *after* a successful collector response (guardrail).
        The ``evidence_id`` from the ``Finding`` is used as the FK reference
        into ``evidence_refs``.
        """
        expires_at = datetime.now(UTC) + _TTL
        async with await psycopg.AsyncConnection.connect(self._postgres_url) as conn:
            await conn.execute(
                """
                INSERT INTO collector_cache
                    (cache_key, incident_id, collector, question,
                     finding_json, evidence_id, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (cache_key) DO UPDATE
                    SET finding_json = EXCLUDED.finding_json,
                        expires_at   = EXCLUDED.expires_at
                """,
                (
                    cache_key,
                    incident_id,
                    collector_name,
                    question,
                    finding.model_dump_json(),
                    finding.evidence_id,
                    expires_at,
                ),
            )
            await conn.commit()
        logger.debug("cache written", extra={"cache_key": cache_key[:16]})
