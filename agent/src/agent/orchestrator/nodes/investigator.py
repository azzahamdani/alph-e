"""Investigator node — LLM-driven hypothesis generation and focus selection.

Reads the current ``IncidentState`` slice (alert + hypotheses + findings),
asks the LLM to propose or update ``Hypothesis`` entries with scores and
evidence references, and picks ``current_focus_hypothesis_id`` for the next
collector dispatch.

Boundary contract (hard rules)
-------------------------------
- Owns: ``hypotheses``, ``current_focus_hypothesis_id``, ``investigation_attempts``,
  ``timeline``, ``updated_at``.
- Never mutates: ``findings``, ``actions_taken``, ``action_intents``, or any
  field belonging to another node.
- Does NOT call collectors — it only selects the focus hypothesis.

Cap behaviour
-------------
When ``investigation_attempts >= 5`` the node returns immediately without
calling the LLM.  The routing function then escalates to the Coordinator.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from typing import Any

import structlog

from agent.llm.client import Client
from agent.llm.observability import LLMCallRecorder
from agent.llm.settings import LLMSettings
from agent.llm.structured import complete_typed
from agent.orchestrator.nodes._models import InvestigatorOutput
from agent.prompts import load as load_prompt
from agent.schemas import IncidentState, TimelineEvent
from agent.schemas.incident import Hypothesis

log = structlog.get_logger(__name__)

_MAX_ATTEMPTS: int = 5


def _content_hash(text: str) -> str:
    """Return a short, stable SHA-256 hex digest of *text*."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _ensure_id(h: Hypothesis) -> Hypothesis:
    """Return *h* with a stable id derived from its text if the id is empty."""
    if h.id:
        return h
    return h.model_copy(update={"id": _content_hash(h.text)})


def _merge_hypotheses(
    existing: list[Hypothesis],
    proposed: list[Hypothesis],
) -> list[Hypothesis]:
    """Merge *proposed* into *existing* using id as the stable key.

    - Proposed entries whose id matches an existing entry replace the old one.
    - Proposed entries with a new id are appended.
    - Existing entries absent from *proposed* are kept unchanged.
    """
    by_id: dict[str, Hypothesis] = {h.id: h for h in existing}
    for p in proposed:
        by_id[p.id] = p
    return list(by_id.values())


def _build_user_message(state: IncidentState) -> str:
    """Serialise the relevant state slice as a JSON user turn."""
    findings_payload = [
        {
            "id": f.id,
            "collector": f.collector_name,
            "question": f.question,
            "summary": f.summary,
            "confidence": f.confidence,
        }
        for f in state.findings
    ]
    hypotheses_payload = [
        {
            "id": h.id,
            "text": h.text,
            "score": h.score,
            "status": h.status,
            "supporting_evidence_ids": h.supporting_evidence_ids,
            "refuting_evidence_ids": h.refuting_evidence_ids,
        }
        for h in state.hypotheses
    ]
    payload: dict[str, Any] = {
        "incident_id": state.incident_id,
        "investigation_attempts": state.investigation_attempts,
        "alert": {
            "source": state.alert.source,
            "raw_message": state.alert.raw_message,
            "service": state.alert.service,
            "severity": state.alert.severity,
            "fired_at": state.alert.fired_at.isoformat(),
            "labels": state.alert.labels,
        },
        "existing_hypotheses": hypotheses_payload,
        "findings": findings_payload,
    }
    return json.dumps(payload, indent=2)


def _get_or_build_client() -> Client:
    """Build a ``Client`` from environment — called lazily so tests can skip it."""
    return Client(
        settings=LLMSettings(),
        api_key=os.environ.get("ANTHROPIC_API_KEY"),
    )


async def investigator_node(
    state: IncidentState,
    *,
    client: Client | None = None,
) -> dict[str, object]:
    """LangGraph node: generate/update hypotheses and pick the next focus.

    Parameters
    ----------
    state:
        The full ``IncidentState`` as provided by LangGraph.
    client:
        Optional pre-built ``Client``.  When ``None`` (production path) a new
        client is constructed from environment variables.  Inject a fake for
        unit tests.
    """
    now = datetime.now(UTC)

    # ------------------------------------------------------------------
    # Attempts cap — bail out without LLM call; routing escalates.
    # ------------------------------------------------------------------
    if state.investigation_attempts >= _MAX_ATTEMPTS:
        log.info(
            "investigator.cap_reached",
            incident_id=state.incident_id,
            attempts=state.investigation_attempts,
        )
        timeline = [
            *state.timeline,
            TimelineEvent(
                ts=now,
                actor="orchestrator:investigator",
                event_type="investigator.cap_reached",
            ),
        ]
        return {
            "investigation_attempts": state.investigation_attempts,
            "timeline": timeline,
            "updated_at": now,
        }

    # ------------------------------------------------------------------
    # Build system prompt (system.md prefix + investigator role prompt).
    # ------------------------------------------------------------------
    bundle = load_prompt("investigator")
    system = f"{bundle.system_prefix}\n\n{bundle.role_prompt}"

    # ------------------------------------------------------------------
    # Build conversation.
    # ------------------------------------------------------------------
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": _build_user_message(state)},
    ]

    # ------------------------------------------------------------------
    # LLM call — wrapped in observability recorder.
    # ------------------------------------------------------------------
    resolved_client = client or _get_or_build_client()

    recorder = LLMCallRecorder(
        model=LLMSettings().model,
        role="investigator",
        incident_id=state.incident_id,
    )

    async with recorder:
        output: InvestigatorOutput = await complete_typed(
            resolved_client,
            system=system,
            messages=messages,
            output_model=InvestigatorOutput,
        )
        # complete_typed returns a validated model; recorder needs a raw Message
        # only for token stats — we skip set_response here because complete_typed
        # does not expose the raw Message.  Observability at the client level
        # (llm.complete log line) still fires.

    # ------------------------------------------------------------------
    # Normalise: ensure every hypothesis has a stable content-hash id.
    # ------------------------------------------------------------------
    proposed = [_ensure_id(h) for h in output.hypotheses]

    # ------------------------------------------------------------------
    # Validate focus id is present in the proposed list.
    # ------------------------------------------------------------------
    proposed_ids = {h.id for h in proposed}
    focus_id = output.current_focus_hypothesis_id
    if focus_id not in proposed_ids:
        log.warning(
            "investigator.focus_id_not_in_hypotheses",
            focus_id=focus_id,
            available=list(proposed_ids),
        )
        # Fallback: pick the hypothesis with the highest score.
        focus_id = max(proposed, key=lambda h: h.score).id if proposed else ""

    # ------------------------------------------------------------------
    # Merge with existing hypotheses (stable across ticks).
    # ------------------------------------------------------------------
    merged = _merge_hypotheses(state.hypotheses, proposed)

    # ------------------------------------------------------------------
    # Build timeline entry.
    # ------------------------------------------------------------------
    timeline = [
        *state.timeline,
        TimelineEvent(
            ts=now,
            actor="orchestrator:investigator",
            event_type="investigator.tick",
        ),
    ]

    log.info(
        "investigator.done",
        incident_id=state.incident_id,
        hypotheses_count=len(merged),
        focus_id=focus_id,
        attempt=state.investigation_attempts + 1,
    )

    # ------------------------------------------------------------------
    # Return only the fields this node owns.
    # ------------------------------------------------------------------
    return {
        "hypotheses": merged,
        "current_focus_hypothesis_id": focus_id,
        "investigation_attempts": state.investigation_attempts + 1,
        "timeline": timeline,
        "updated_at": now,
    }


__all__ = ["investigator_node"]
