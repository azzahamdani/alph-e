"""Collectors dispatch node — fans out to Go collector HTTP services.

Builds a ``CollectorInput`` from the current focus hypothesis, picks the right
Go collector via the selection registry, checks the Postgres cache, and (on a
miss) POSTs to ``/collect``. The resulting ``Finding`` is merged into
``IncidentState.findings``.

Boundary contract
-----------------
Owns: ``findings``, ``services_touched``, ``timeline``, ``updated_at``.
Never mutates: ``hypotheses``, ``current_focus_hypothesis_id``,
               ``investigation_attempts``, ``actions_taken``, ``action_intents``.

Guardrails
----------
- Never blocks the graph for more than 30 s per collector (enforced in
  ``dispatch.http`` via ``httpx.Timeout(30.0)``).
- Cache misses are persisted *after* a successful response, never before.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

import httpx

from agent.orchestrator.dispatch import (
    CollectorCache,
    dispatch,
    make_cache_key,
    select_collector,
)
from agent.schemas import (
    CollectorInput,
    CollectorOutput,
    EnvironmentFingerprint,
    Finding,
    IncidentState,
    TimelineEvent,
    TimeRange,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default environment fingerprint values (overridable via env vars)
# ---------------------------------------------------------------------------

_DEFAULT_CLUSTER = "k3d-devops-lab"
_DEFAULT_ACCOUNT = "local"
_DEFAULT_REGION = "local"

# Time window: alert-fired-at minus 10 min → now
_LOOK_BACK = timedelta(minutes=10)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_time_range(state: IncidentState) -> TimeRange:
    """Derive a [start, end) time range anchored on the alert fired_at time."""
    end = datetime.now(UTC)
    start = state.alert.fired_at - _LOOK_BACK
    return TimeRange(start=start, end=end)


def _build_env_fingerprint() -> EnvironmentFingerprint:
    """Read env fingerprint from env vars; fall back to safe defaults."""
    return EnvironmentFingerprint(
        cluster=os.environ.get("ENV_CLUSTER", _DEFAULT_CLUSTER),
        account=os.environ.get("ENV_ACCOUNT", _DEFAULT_ACCOUNT),
        region=os.environ.get("ENV_REGION", _DEFAULT_REGION),
        deploy_revision=os.environ.get("ENV_DEPLOY_REVISION", "unknown"),
        rollout_generation=os.environ.get("ENV_ROLLOUT_GENERATION", "0"),
    )


def _scope_services(state: IncidentState) -> list[str]:
    """Derive the scope services list: alert service + services_touched so far."""
    seen: set[str] = {state.alert.service}
    seen.update(state.services_touched)
    return sorted(seen)


def _merge_services_touched(existing: list[str], new_services: list[str]) -> list[str]:
    """Return deduplicated union of existing and new services, sorted."""
    merged: set[str] = set(existing) | set(new_services)
    return sorted(merged)


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------


async def collectors_node(
    state: IncidentState,
    *,
    http_client: httpx.AsyncClient | None = None,
    cache: CollectorCache | None = None,
) -> dict[str, object]:
    """LangGraph node: dispatch to the appropriate Go collector.

    Parameters
    ----------
    state:
        Full ``IncidentState`` as provided by LangGraph.
    http_client:
        Optional pre-built ``httpx.AsyncClient``.  Injected by tests to avoid
        real network calls.
    cache:
        Optional pre-built ``CollectorCache``.  Injected by tests to avoid
        Postgres.  When ``None`` and ``POSTGRES_URL`` is set, a real cache is
        constructed; when ``POSTGRES_URL`` is absent, caching is skipped.
    """
    now = datetime.now(UTC)

    # ------------------------------------------------------------------
    # Resolve the focus hypothesis
    # ------------------------------------------------------------------
    focus_id = state.current_focus_hypothesis_id
    focus_hypothesis = next(
        (h for h in state.hypotheses if h.id == focus_id),
        None,
    )

    if focus_hypothesis is None:
        # No hypothesis to investigate yet — emit a skeleton event and return.
        logger.warning(
            "collectors_node: no focus hypothesis",
            extra={"incident_id": state.incident_id},
        )
        timeline = [
            *state.timeline,
            TimelineEvent(
                ts=now,
                actor="orchestrator:collectors",
                event_type="collectors.dispatch.no_focus",
            ),
        ]
        return {"timeline": timeline, "updated_at": now}

    # ------------------------------------------------------------------
    # Build collector input
    # ------------------------------------------------------------------
    collector_name = select_collector(focus_hypothesis.text)
    time_range = _build_time_range(state)
    env_fingerprint = _build_env_fingerprint()
    scope = _scope_services(state)

    collector_input = CollectorInput(
        incident_id=state.incident_id,
        question=focus_hypothesis.text,
        hypothesis_id=focus_hypothesis.id,
        time_range=time_range,
        scope_services=scope,
        environment_fingerprint=env_fingerprint,
    )

    # ------------------------------------------------------------------
    # Cache lookup
    # ------------------------------------------------------------------
    cache_key = make_cache_key(
        incident_id=state.incident_id,
        collector_name=collector_name,
        question=focus_hypothesis.text,
        time_range=time_range,
        scope_services=scope,
        environment_fingerprint=env_fingerprint,
    )

    postgres_url = os.environ.get("POSTGRES_URL", "")
    resolved_cache = cache if cache is not None else (
        CollectorCache(postgres_url) if postgres_url else None
    )

    cached_finding: Finding | None = None
    if resolved_cache is not None:
        try:
            cached_finding = await resolved_cache.get(cache_key)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "collectors_node: cache get failed",
                extra={"incident_id": state.incident_id, "error": str(exc)},
            )

    if cached_finding is not None:
        logger.info(
            "collectors_node: cache hit",
            extra={
                "incident_id": state.incident_id,
                "collector": collector_name,
                "cache_key": cache_key[:16],
            },
        )
        finding = cached_finding
        event_type = "collectors.dispatch.cache_hit"
    else:
        # ------------------------------------------------------------------
        # HTTP dispatch
        # ------------------------------------------------------------------
        result: CollectorOutput | Finding = await dispatch(
            collector_name,
            collector_input,
            http_client=http_client,
        )

        if isinstance(result, CollectorOutput):
            finding = result.finding
            event_type = "collectors.dispatch.success"

            # Persist to cache AFTER successful response (guardrail)
            if resolved_cache is not None:
                try:
                    await resolved_cache.put(
                        cache_key=cache_key,
                        incident_id=state.incident_id,
                        collector_name=collector_name,
                        question=focus_hypothesis.text,
                        finding=finding,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "collectors_node: cache put failed",
                        extra={"incident_id": state.incident_id, "error": str(exc)},
                    )
        else:
            # result is already a failure Finding
            finding = result
            event_type = "collectors.dispatch.failed"

    # ------------------------------------------------------------------
    # Merge finding into state
    # ------------------------------------------------------------------
    existing_ids = {f.id for f in state.findings}
    new_findings = [*state.findings] if finding.id in existing_ids else [
        *state.findings,
        finding,
    ]

    # ------------------------------------------------------------------
    # Update services_touched
    # ------------------------------------------------------------------
    new_services_touched = _merge_services_touched(state.services_touched, scope)

    # ------------------------------------------------------------------
    # Emit timeline event
    # ------------------------------------------------------------------
    timeline = [
        *state.timeline,
        TimelineEvent(
            ts=now,
            actor=f"orchestrator:collectors:{collector_name}",
            event_type=event_type,
            ref_id=finding.id,
        ),
    ]

    logger.info(
        "collectors_node.done",
        extra={
            "incident_id": state.incident_id,
            "collector": collector_name,
            "finding_id": finding.id,
            "confidence": finding.confidence,
            "event_type": event_type,
        },
    )

    return {
        "findings": new_findings,
        "services_touched": new_services_touched,
        "timeline": timeline,
        "updated_at": now,
    }
