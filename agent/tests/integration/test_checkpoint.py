"""Integration test: Postgres checkpointer persists ``IncidentState``.

Requires a live Postgres instance reachable at localhost:5432
(``task agent-infra:postgres`` port-forward in a separate terminal).

Run::

    uv run pytest tests/integration/test_checkpoint.py -q -m integration

What this proves:
1. The graph runs Intake → Investigator → (collectors loop) → Coordinator → END
   and the final ``IncidentState.phase`` is persisted to Postgres.
2. A second call with the same ``incident_id`` hits the checkpoint and returns
   the persisted state without re-executing any node (short-circuit).
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import psycopg
import pytest

from agent.orchestrator.checkpoint import postgres_checkpointer
from agent.orchestrator.run import run_once
from agent.schemas import Alert, IncidentPhase, IncidentState, Severity

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_POSTGRES_URL = "postgresql://devops:devops@localhost:5432/devops"
# AsyncPostgresSaver requires the asyncpg driver.
_DEFAULT_ASYNC_POSTGRES_URL = "postgresql+asyncpg://devops:devops@localhost:5432/devops"


def _postgres_url() -> str:
    return os.environ.get("POSTGRES_URL", _DEFAULT_ASYNC_POSTGRES_URL)


def _sync_postgres_url() -> str:
    """Plain psycopg URL for availability probe (no asyncpg prefix)."""
    url = _postgres_url()
    return url.replace("postgresql+asyncpg://", "postgresql://")


async def _postgres_available() -> bool:
    try:
        async with await psycopg.AsyncConnection.connect(
            _sync_postgres_url(), connect_timeout=2
        ):
            return True
    except Exception:  # noqa: BLE001
        return False


def _seed_state(incident_id: str) -> IncidentState:
    now = datetime.now(UTC)
    return IncidentState(
        incident_id=incident_id,
        alert=Alert(
            source="alertmanager",
            raw_message="PodOOMKilled: leaky-service",
            service="leaky-service",
            severity=Severity.high,
            fired_at=now,
            labels={"namespace": "demo", "pod": "leaky-service-abc123"},
        ),
        phase=IncidentPhase.intake,
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
async def _require_postgres() -> None:
    """Skip the whole module if Postgres is not reachable."""
    if not await _postgres_available():
        pytest.skip(
            "Postgres not reachable — run `task agent-infra:postgres` first "
            "(port-forward on localhost:5432)"
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_graph_persists_phase_to_postgres() -> None:
    """Graph runs to END and final phase is readable from the checkpoint."""
    incident_id = f"test-{uuid.uuid4().hex}"
    seed = _seed_state(incident_id)

    async with postgres_checkpointer(_postgres_url()) as saver:
        final = await run_once(seed, checkpointer=saver)

    # The skeleton investigator hits MAX_INVESTIGATION_ATTEMPTS (5) and routes
    # to coordinator which sets phase=escalated.
    assert final.phase == IncidentPhase.escalated
    assert final.incident_id == incident_id
    # Timeline should include at least intake + investigator events.
    event_types = {e.event_type for e in final.timeline}
    assert "intake.accepted" in event_types
    assert "investigator.tick" in event_types


@pytest.mark.integration
async def test_second_run_reuses_checkpoint() -> None:
    """A second call with the same incident_id reads from checkpoint, not re-runs.

    We verify this by counting ``investigator.tick`` events: if nodes re-ran,
    there would be more than MAX_INVESTIGATION_ATTEMPTS (5) ticks.
    """
    incident_id = f"test-{uuid.uuid4().hex}"
    seed = _seed_state(incident_id)

    async with postgres_checkpointer(_postgres_url()) as saver:
        first = await run_once(seed, checkpointer=saver)
        first_tick_count = sum(
            1 for e in first.timeline if e.event_type == "investigator.tick"
        )

        # Second call: same incident_id, same checkpointer session.
        second = await run_once(seed, checkpointer=saver)

    second_tick_count = sum(
        1 for e in second.timeline if e.event_type == "investigator.tick"
    )

    assert second.phase == first.phase == IncidentPhase.escalated
    # No additional investigator ticks — second run was a no-op read.
    assert second_tick_count == first_tick_count, (
        f"Second run re-executed nodes: tick count grew from "
        f"{first_tick_count} to {second_tick_count}"
    )


@pytest.mark.integration
async def test_checkpoint_survives_new_saver_instance() -> None:
    """State read via a fresh checkpointer connection (simulates process restart)."""
    incident_id = f"test-{uuid.uuid4().hex}"
    seed = _seed_state(incident_id)

    # First "process": run graph and close the checkpointer.
    async with postgres_checkpointer(_postgres_url()) as saver:
        first = await run_once(seed, checkpointer=saver)

    assert first.phase == IncidentPhase.escalated

    # Second "process": open a fresh connection and read back the checkpoint.
    async with postgres_checkpointer(_postgres_url()) as saver2:
        second = await run_once(seed, checkpointer=saver2)

    assert second.phase == IncidentPhase.escalated
    assert second.incident_id == incident_id
    # Tick count must be identical — no new nodes fired on "restart".
    first_ticks = sum(1 for e in first.timeline if e.event_type == "investigator.tick")
    second_ticks = sum(1 for e in second.timeline if e.event_type == "investigator.tick")
    assert first_ticks == second_ticks, (
        "Checkpoint was not reused after simulated restart: "
        f"ticks went from {first_ticks} to {second_ticks}"
    )
