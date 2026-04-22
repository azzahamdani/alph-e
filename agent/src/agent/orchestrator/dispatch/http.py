"""Async HTTP client for Go collector services.

Each collector exposes a single ``POST /collect`` endpoint that accepts a
``CollectorInput`` JSON body and returns a ``CollectorOutput`` JSON body.

Endpoint discovery:

    COLLECTOR_PROM_URL  → prom-collector
    COLLECTOR_LOKI_URL  → loki-collector
    COLLECTOR_KUBE_URL  → kube-collector

Defaults to ``http://localhost:808{1,2,3}`` if the env vars are absent (useful
for local development without k3d).

In-cluster, the agent Deployment sets these env vars explicitly to the
collector ``Service`` DNS names (e.g. ``http://prom-collector.agent.svc:8001``),
so the localhost defaults are never hit.

Timeout: 30s hard cap per collector call (B-02 guardrail).
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

import httpx

from agent.schemas.collector import (
    CollectorInput,
    CollectorOutput,
    Finding,
)

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(30.0)

# ---------------------------------------------------------------------------
# Endpoint registry
# ---------------------------------------------------------------------------

_ENV_MAP: dict[str, str] = {
    "prom": "COLLECTOR_PROM_URL",
    "loki": "COLLECTOR_LOKI_URL",
    "kube": "COLLECTOR_KUBE_URL",
}

_DEFAULT_URLS: dict[str, str] = {
    "prom": "http://localhost:8081",
    "loki": "http://localhost:8082",
    "kube": "http://localhost:8083",
}


def _endpoint_for(collector_name: str) -> str:
    env_var = _ENV_MAP.get(collector_name, "COLLECTOR_PROM_URL")
    default = _DEFAULT_URLS.get(collector_name, "http://localhost:8081")
    return os.environ.get(env_var, default)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def dispatch(
    collector_name: str,
    payload: CollectorInput,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> CollectorOutput | Finding:
    """POST *payload* to the named collector and return the parsed output.

    On non-2xx response the function returns a failure ``Finding`` (confidence
    0.0) rather than raising, so the graph can continue without hard-failing.

    Parameters
    ----------
    collector_name:
        One of ``"prom"``, ``"loki"``, ``"kube"``.
    payload:
        Fully-populated ``CollectorInput`` to send.
    http_client:
        Optional pre-constructed client; useful for tests. When ``None`` a new
        client with a 30s timeout is created per call.

    Returns
    -------
    CollectorOutput
        On success (2xx response with valid body).
    Finding
        On HTTP error or parse failure (``confidence=0.0``).
    """
    endpoint = _endpoint_for(collector_name)
    url = f"{endpoint}/collect"

    body = payload.model_dump_json()
    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    async def _call(client: httpx.AsyncClient) -> CollectorOutput | Finding:
        try:
            resp = await client.post(url, content=body, headers=headers)
        except httpx.TimeoutException as exc:
            return _failure_finding(
                payload=payload,
                collector_name=collector_name,
                reason=f"timeout after 30s: {exc}",
            )
        except httpx.HTTPError as exc:
            return _failure_finding(
                payload=payload,
                collector_name=collector_name,
                reason=f"HTTP error: {exc}",
            )

        if not resp.is_success:
            return _failure_finding(
                payload=payload,
                collector_name=collector_name,
                reason=f"non-200 status {resp.status_code}: {resp.text[:200]}",
            )

        try:
            return CollectorOutput.model_validate_json(resp.text)
        except Exception as exc:  # noqa: BLE001
            return _failure_finding(
                payload=payload,
                collector_name=collector_name,
                reason=f"response parse error: {exc}",
            )

    if http_client is not None:
        return await _call(http_client)

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        return await _call(client)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _failure_finding(
    *,
    payload: CollectorInput,
    collector_name: str,
    reason: str,
) -> Finding:
    logger.warning(
        "collector dispatch failed",
        extra={
            "collector": collector_name,
            "incident_id": payload.incident_id,
            "reason": reason,
        },
    )
    return Finding(
        id=f"fail_{payload.hypothesis_id}_{collector_name}",
        collector_name=collector_name,
        question=payload.question,
        summary=f"Collector {collector_name!r} failed: {reason}",
        evidence_id="",
        confidence=0.0,
        created_at=datetime.now(UTC),
    )
