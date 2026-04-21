"""Unit tests for agent.orchestrator.nodes.investigator.

All LLM calls are faked — no real network traffic.

Coverage
--------
- cap-respect: node returns without LLM call when attempts >= 5
- hypothesis merge: existing hypotheses are retained and updated entries win
- focus-id selection: focus id from LLM output is honoured; fallback to
  highest-score hypothesis when focus id is absent
- attempts counter: incremented on normal tick, unchanged on cap-hit
- no boundary violations: findings, actions_taken never present in return dict
- stable ids: content hash assigned when hypothesis id is empty
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.orchestrator.nodes.investigator import (
    _content_hash,
    _ensure_id,
    _merge_hypotheses,
    investigator_node,
)
from agent.schemas.incident import (
    Alert,
    Hypothesis,
    HypothesisStatus,
    IncidentState,
    Severity,
)

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 21, 12, 0, 0, tzinfo=UTC)


def _make_alert() -> Alert:
    return Alert(
        source="alertmanager",
        raw_message="OOMKilled leaky-service",
        service="leaky-service",
        severity=Severity.critical,
        fired_at=_NOW,
        labels={"namespace": "demo"},
    )


def _make_state(
    *,
    attempts: int = 0,
    hypotheses: list[Hypothesis] | None = None,
) -> IncidentState:
    return IncidentState(
        incident_id="inc-001",
        alert=_make_alert(),
        hypotheses=hypotheses or [],
        investigation_attempts=attempts,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _make_hypothesis(
    *,
    hyp_id: str = "h-001",
    text: str = "Memory leak in leaky-service",
    score: float = 0.7,
    status: HypothesisStatus = HypothesisStatus.open,
) -> Hypothesis:
    return Hypothesis(
        id=hyp_id,
        text=text,
        score=score,
        status=status,
        created_at=_NOW,
    )


def _h_as_dict(h: Hypothesis) -> dict[str, Any]:
    """Serialise a Hypothesis to a dict with native Python types.

    Pydantic strict mode validates the tool-use input dict directly; ``created_at``
    must be a ``datetime`` (not a string) because strict mode disables coercion.
    """
    return {
        "id": h.id,
        "text": h.text,
        "score": h.score,
        "status": h.status,
        "supporting_evidence_ids": list(h.supporting_evidence_ids),
        "refuting_evidence_ids": list(h.refuting_evidence_ids),
        "created_at": h.created_at,  # datetime object — strict mode requires this
    }


def _make_tool_use_message(tool_name: str, payload: dict[str, Any]) -> MagicMock:
    """Build a fake anthropic Message with a single ToolUseBlock."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = tool_name
    block.input = payload

    msg = MagicMock()
    msg.content = [block]
    usage = MagicMock()
    usage.input_tokens = 100
    usage.output_tokens = 50
    usage.cache_creation_input_tokens = 0
    usage.cache_read_input_tokens = 0
    msg.usage = usage
    msg.model = "claude-sonnet-4-6"
    return msg


def _make_fake_client(
    hypotheses: list[Hypothesis],
    focus_id: str,
    reasoning: str = "Test reasoning text.",
) -> MagicMock:
    """Return a mock Client that returns a valid InvestigatorOutput."""
    output_payload: dict[str, Any] = {
        "hypotheses": [_h_as_dict(h) for h in hypotheses],
        "current_focus_hypothesis_id": focus_id,
        "reasoning": reasoning,
    }
    msg = _make_tool_use_message("investigatoroutput", output_payload)
    client = MagicMock()
    client.complete = AsyncMock(return_value=msg)
    return client


# ---------------------------------------------------------------------------
# Unit tests for private helpers
# ---------------------------------------------------------------------------


def test_content_hash_is_stable() -> None:
    text = "memory leak in leaky-service"
    h1 = _content_hash(text)
    h2 = _content_hash(text)
    assert h1 == h2
    assert len(h1) == 16
    assert h1 == hashlib.sha256(text.encode()).hexdigest()[:16]


def test_content_hash_differs_for_different_text() -> None:
    assert _content_hash("abc") != _content_hash("xyz")


def test_ensure_id_preserves_existing_id() -> None:
    h = _make_hypothesis(hyp_id="stable-id")
    result = _ensure_id(h)
    assert result.id == "stable-id"


def test_ensure_id_assigns_content_hash_when_empty() -> None:
    h = Hypothesis(id="", text="oom leak", score=0.5, created_at=_NOW)
    result = _ensure_id(h)
    assert result.id == _content_hash("oom leak")
    assert result.text == "oom leak"


def test_merge_hypotheses_new_entry_appended() -> None:
    existing = [_make_hypothesis(hyp_id="h-001")]
    proposed = [_make_hypothesis(hyp_id="h-002", text="new hypothesis")]
    merged = _merge_hypotheses(existing, proposed)
    ids = {h.id for h in merged}
    assert "h-001" in ids
    assert "h-002" in ids


def test_merge_hypotheses_proposed_replaces_existing() -> None:
    existing = [_make_hypothesis(hyp_id="h-001", score=0.3)]
    proposed = [_make_hypothesis(hyp_id="h-001", score=0.9)]
    merged = _merge_hypotheses(existing, proposed)
    assert len(merged) == 1
    assert merged[0].score == pytest.approx(0.9)


def test_merge_hypotheses_absent_existing_kept() -> None:
    existing = [_make_hypothesis(hyp_id="h-001"), _make_hypothesis(hyp_id="h-002")]
    proposed = [_make_hypothesis(hyp_id="h-001", score=0.95)]  # only update h-001
    merged = _merge_hypotheses(existing, proposed)
    ids = {h.id for h in merged}
    assert "h-001" in ids
    assert "h-002" in ids  # h-002 preserved


# ---------------------------------------------------------------------------
# Cap behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cap_reached_returns_without_llm_call() -> None:
    """When attempts >= 5 the node must not call the LLM."""
    state = _make_state(attempts=5)
    fake_client = MagicMock()
    fake_client.complete = AsyncMock()

    result = await investigator_node(state, client=fake_client)

    fake_client.complete.assert_not_awaited()
    assert result["investigation_attempts"] == 5  # unchanged


@pytest.mark.asyncio
async def test_cap_at_exactly_five() -> None:
    state = _make_state(attempts=5)
    fake_client = MagicMock()
    fake_client.complete = AsyncMock()

    result = await investigator_node(state, client=fake_client)

    assert result["investigation_attempts"] == 5
    assert "timeline" in result
    assert "hypotheses" not in result  # not returned on cap path


@pytest.mark.asyncio
async def test_cap_at_six_also_respected() -> None:
    state = _make_state(attempts=6)
    fake_client = MagicMock()
    fake_client.complete = AsyncMock()

    await investigator_node(state, client=fake_client)

    fake_client.complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_cap_timeline_event_type() -> None:
    state = _make_state(attempts=5)
    fake_client = MagicMock()
    fake_client.complete = AsyncMock()

    result = await investigator_node(state, client=fake_client)

    events = result["timeline"]
    assert isinstance(events, list)
    last = events[-1]
    assert last.event_type == "investigator.cap_reached"


# ---------------------------------------------------------------------------
# Normal tick — attempts increment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attempts_incremented_on_normal_tick() -> None:
    h = _make_hypothesis(hyp_id="h-001")
    state = _make_state(attempts=0)
    fake_client = _make_fake_client([h], focus_id="h-001")

    result = await investigator_node(state, client=fake_client)

    assert result["investigation_attempts"] == 1


@pytest.mark.asyncio
async def test_attempts_incremented_from_nonzero() -> None:
    h = _make_hypothesis(hyp_id="h-001")
    state = _make_state(attempts=3)
    fake_client = _make_fake_client([h], focus_id="h-001")

    result = await investigator_node(state, client=fake_client)

    assert result["investigation_attempts"] == 4


# ---------------------------------------------------------------------------
# Hypothesis merge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_hypotheses_added() -> None:
    state = _make_state(attempts=0)
    h = _make_hypothesis(hyp_id="h-new", text="OOM from leak")
    fake_client = _make_fake_client([h], focus_id="h-new")

    result = await investigator_node(state, client=fake_client)

    merged: list[Hypothesis] = result["hypotheses"]  # type: ignore[assignment]
    ids = {hyp.id for hyp in merged}
    assert "h-new" in ids


@pytest.mark.asyncio
async def test_existing_hypothesis_updated() -> None:
    existing = _make_hypothesis(hyp_id="h-001", score=0.2)
    state = _make_state(attempts=0, hypotheses=[existing])
    updated = _make_hypothesis(hyp_id="h-001", score=0.9)
    fake_client = _make_fake_client([updated], focus_id="h-001")

    result = await investigator_node(state, client=fake_client)

    merged: list[Hypothesis] = result["hypotheses"]  # type: ignore[assignment]
    found = next(hyp for hyp in merged if hyp.id == "h-001")
    assert found.score == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_existing_not_in_proposed_is_retained() -> None:
    h1 = _make_hypothesis(hyp_id="h-001")
    h2 = _make_hypothesis(hyp_id="h-002", text="different cause")
    state = _make_state(attempts=0, hypotheses=[h1, h2])
    # LLM only returns h-001 in the proposed list
    fake_client = _make_fake_client([h1], focus_id="h-001")

    result = await investigator_node(state, client=fake_client)

    merged: list[Hypothesis] = result["hypotheses"]  # type: ignore[assignment]
    ids = {hyp.id for hyp in merged}
    assert "h-001" in ids
    assert "h-002" in ids  # retained because it was existing


# ---------------------------------------------------------------------------
# Focus-id selection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_focus_id_from_llm_output_honoured() -> None:
    h1 = _make_hypothesis(hyp_id="h-001", score=0.5)
    h2 = _make_hypothesis(hyp_id="h-002", score=0.9)
    state = _make_state(attempts=0)
    fake_client = _make_fake_client([h1, h2], focus_id="h-001")

    result = await investigator_node(state, client=fake_client)

    assert result["current_focus_hypothesis_id"] == "h-001"


@pytest.mark.asyncio
async def test_focus_id_falls_back_to_highest_score_when_missing() -> None:
    """If the LLM emits a focus_id not in the hypotheses list, use the highest-score one."""
    h1 = _make_hypothesis(hyp_id="h-001", score=0.3)
    h2 = _make_hypothesis(hyp_id="h-002", score=0.9)
    state = _make_state(attempts=0)
    # Focus id "h-999" is not in the proposed list
    fake_client = _make_fake_client([h1, h2], focus_id="h-999")

    result = await investigator_node(state, client=fake_client)

    assert result["current_focus_hypothesis_id"] == "h-002"


@pytest.mark.asyncio
async def test_focus_id_empty_string_when_no_hypotheses() -> None:
    """If no hypotheses are returned and focus_id is invalid, default to empty string."""
    state = _make_state(attempts=0)
    # LLM returns no hypotheses and an invalid focus id
    output_payload: dict[str, Any] = {
        "hypotheses": [],
        "current_focus_hypothesis_id": "h-does-not-exist",
        "reasoning": "No hypotheses formed yet.",
    }
    msg = _make_tool_use_message("investigatoroutput", output_payload)
    fake_client = MagicMock()
    fake_client.complete = AsyncMock(return_value=msg)

    result = await investigator_node(state, client=fake_client)

    assert result["current_focus_hypothesis_id"] == ""


# ---------------------------------------------------------------------------
# No boundary violations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_findings_in_return() -> None:
    h = _make_hypothesis(hyp_id="h-001")
    state = _make_state(attempts=0)
    fake_client = _make_fake_client([h], focus_id="h-001")

    result = await investigator_node(state, client=fake_client)

    assert "findings" not in result


@pytest.mark.asyncio
async def test_no_actions_taken_in_return() -> None:
    h = _make_hypothesis(hyp_id="h-001")
    state = _make_state(attempts=0)
    fake_client = _make_fake_client([h], focus_id="h-001")

    result = await investigator_node(state, client=fake_client)

    assert "actions_taken" not in result


@pytest.mark.asyncio
async def test_no_action_intents_in_return() -> None:
    h = _make_hypothesis(hyp_id="h-001")
    state = _make_state(attempts=0)
    fake_client = _make_fake_client([h], focus_id="h-001")

    result = await investigator_node(state, client=fake_client)

    assert "action_intents" not in result


@pytest.mark.asyncio
async def test_no_phase_in_return() -> None:
    h = _make_hypothesis(hyp_id="h-001")
    state = _make_state(attempts=0)
    fake_client = _make_fake_client([h], focus_id="h-001")

    result = await investigator_node(state, client=fake_client)

    assert "phase" not in result


# ---------------------------------------------------------------------------
# Stable ids — content hash assignment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hypothesis_without_id_gets_content_hash() -> None:
    """An LLM-returned hypothesis with empty id must receive a stable content hash."""
    text = "memory leak from background goroutine"
    state = _make_state(attempts=0)
    h_no_id = Hypothesis(id="", text=text, score=0.6, created_at=_NOW)
    expected_id = _content_hash(text)
    # Focus id matches the hash-derived id
    output_payload: dict[str, Any] = {
        "hypotheses": [_h_as_dict(h_no_id)],
        "current_focus_hypothesis_id": expected_id,
        "reasoning": "hash-derived id test",
    }
    msg = _make_tool_use_message("investigatoroutput", output_payload)
    fake_client = MagicMock()
    fake_client.complete = AsyncMock(return_value=msg)

    result = await investigator_node(state, client=fake_client)

    merged: list[Hypothesis] = result["hypotheses"]  # type: ignore[assignment]
    assert len(merged) == 1
    assert merged[0].id == expected_id


@pytest.mark.asyncio
async def test_hypothesis_id_stable_across_ticks() -> None:
    """The same text must produce the same id on repeated calls."""
    text = "OOM caused by unbounded cache"
    expected_id = _content_hash(text)
    h_no_id = Hypothesis(id="", text=text, score=0.5, created_at=_NOW)

    state = _make_state(attempts=0)
    output_payload: dict[str, Any] = {
        "hypotheses": [_h_as_dict(h_no_id)],
        "current_focus_hypothesis_id": expected_id,
        "reasoning": "stability test",
    }
    msg = _make_tool_use_message("investigatoroutput", output_payload)
    fake_client = MagicMock()
    fake_client.complete = AsyncMock(return_value=msg)

    result1 = await investigator_node(state, client=fake_client)
    merged1: list[Hypothesis] = result1["hypotheses"]  # type: ignore[assignment]

    # Second tick — same text, hypothesis already in state with assigned id
    state2 = _make_state(attempts=1, hypotheses=merged1)
    # Now h has the hash-assigned id
    h_with_id = next(h for h in merged1 if h.text == text)
    output_payload2: dict[str, Any] = {
        "hypotheses": [_h_as_dict(h_with_id)],
        "current_focus_hypothesis_id": expected_id,
        "reasoning": "stability test tick 2",
    }
    msg2 = _make_tool_use_message("investigatoroutput", output_payload2)
    fake_client2 = MagicMock()
    fake_client2.complete = AsyncMock(return_value=msg2)

    result2 = await investigator_node(state2, client=fake_client2)

    merged2: list[Hypothesis] = result2["hypotheses"]  # type: ignore[assignment]
    matching = [h for h in merged2 if h.text == text]
    assert len(matching) == 1
    assert matching[0].id == expected_id


# ---------------------------------------------------------------------------
# Timeline event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeline_event_emitted_on_normal_tick() -> None:
    h = _make_hypothesis(hyp_id="h-001")
    state = _make_state(attempts=0)
    fake_client = _make_fake_client([h], focus_id="h-001")

    result = await investigator_node(state, client=fake_client)

    events = result["timeline"]
    assert isinstance(events, list)
    last = events[-1]
    assert last.event_type == "investigator.tick"
    assert last.actor == "orchestrator:investigator"


@pytest.mark.asyncio
async def test_updated_at_is_set() -> None:
    h = _make_hypothesis(hyp_id="h-001")
    state = _make_state(attempts=0)
    fake_client = _make_fake_client([h], focus_id="h-001")

    result = await investigator_node(state, client=fake_client)

    assert isinstance(result["updated_at"], datetime)
