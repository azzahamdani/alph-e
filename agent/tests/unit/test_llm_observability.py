"""Unit tests for agent.llm.observability.

Rules:
- No real LLM calls — anthropic.types.Message is constructed directly.
- structlog is configured to capture events via a ListProcessor.
- All assertions target the emitted log event dict and RunStats.
"""

from __future__ import annotations

import pytest
import structlog
from anthropic.types import Message, Usage

from agent.llm.observability import (
    DEFAULT_PRICING,
    LLMCallRecorder,
    RunStats,
    _estimate_cost,
    record_llm_call,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MODEL = "claude-sonnet-4-6"
_ROLE = "Investigator"
_INCIDENT = "inc_abc123"


def _make_message(
    *,
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> Message:
    """Build a minimal anthropic.types.Message with the given usage fields."""
    usage = Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
    )
    return Message(
        id="msg_test",
        type="message",
        role="assistant",
        content=[],
        model=_MODEL,
        stop_reason="end_turn",
        stop_sequence=None,
        usage=usage,
    )


# ---------------------------------------------------------------------------
# Pricing table unit tests
# ---------------------------------------------------------------------------


def test_pricing_table_contains_mvp1_model() -> None:
    assert _MODEL in DEFAULT_PRICING
    rates = DEFAULT_PRICING[_MODEL]
    assert "input" in rates
    assert "output" in rates
    assert "cache_write" in rates
    assert "cache_read" in rates


def test_estimate_cost_known_model() -> None:
    cost = _estimate_cost(
        model=_MODEL,
        input_tokens=1_000_000,
        output_tokens=0,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        pricing=DEFAULT_PRICING,
    )
    assert cost == pytest.approx(3.00, rel=1e-6)


def test_estimate_cost_unknown_model_returns_zero() -> None:
    cost = _estimate_cost(
        model="unknown-model-xyz",
        input_tokens=999,
        output_tokens=999,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        pricing=DEFAULT_PRICING,
    )
    assert cost == 0.0


def test_estimate_cost_all_token_types() -> None:
    rates = DEFAULT_PRICING[_MODEL]
    cost = _estimate_cost(
        model=_MODEL,
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_creation_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        pricing=DEFAULT_PRICING,
    )
    expected = rates["input"] + rates["output"] + rates["cache_write"] + rates["cache_read"]
    assert cost == pytest.approx(expected, rel=1e-6)


# ---------------------------------------------------------------------------
# RunStats unit tests
# ---------------------------------------------------------------------------


def test_run_stats_initial_zeros() -> None:
    stats = RunStats()
    assert stats.calls == 0
    assert stats.input_tokens == 0
    assert stats.errors == 0
    assert stats.est_cost_usd == 0.0


def test_run_stats_accumulates_on_success() -> None:
    stats = RunStats()
    stats.record_success(
        input_tokens=100,
        output_tokens=50,
        cache_creation_tokens=10,
        cache_read_tokens=5,
        est_cost_usd=0.001,
    )
    stats.record_success(
        input_tokens=200,
        output_tokens=80,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        est_cost_usd=0.002,
    )
    assert stats.calls == 2
    assert stats.input_tokens == 300
    assert stats.output_tokens == 130
    assert stats.cache_creation_tokens == 10
    assert stats.cache_read_tokens == 5
    assert stats.est_cost_usd == pytest.approx(0.003, rel=1e-6)
    assert stats.errors == 0


def test_run_stats_record_error() -> None:
    stats = RunStats()
    stats.record_error()
    assert stats.calls == 1
    assert stats.errors == 1
    assert stats.input_tokens == 0


# ---------------------------------------------------------------------------
# LLMCallRecorder — success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recorder_success_emits_llm_call_event() -> None:
    with structlog.testing.capture_logs() as cap:
        stats = RunStats()
        async with LLMCallRecorder(
            model=_MODEL,
            role=_ROLE,
            incident_id=_INCIDENT,
            run_stats=stats,
        ) as r:
            msg = _make_message(
                input_tokens=100,
                output_tokens=50,
                cache_creation_input_tokens=20,
                cache_read_input_tokens=5,
            )
            r.set_response(msg)

    assert len(cap) == 1
    event = cap[0]

    # Required keys per spec
    assert event["event"] == "llm.call"
    assert event["model"] == _MODEL
    assert event["role"] == _ROLE
    assert event["incident_id"] == _INCIDENT
    assert event["input_tokens"] == 100
    assert event["output_tokens"] == 50
    assert event["cache_creation_tokens"] == 20
    assert event["cache_read_tokens"] == 5
    assert "latency_ms" in event
    assert isinstance(event["latency_ms"], float)
    assert "est_cost_usd" in event
    assert isinstance(event["est_cost_usd"], float)
    assert event["est_cost_usd"] > 0.0
    # No error key on success
    assert "error" not in event


@pytest.mark.asyncio
async def test_recorder_success_updates_run_stats() -> None:
    stats = RunStats()
    with structlog.testing.capture_logs():
        async with LLMCallRecorder(
            model=_MODEL,
            role=_ROLE,
            run_stats=stats,
        ) as r:
            r.set_response(_make_message(input_tokens=200, output_tokens=80))

    assert stats.calls == 1
    assert stats.input_tokens == 200
    assert stats.output_tokens == 80
    assert stats.errors == 0
    assert stats.est_cost_usd > 0.0


# ---------------------------------------------------------------------------
# LLMCallRecorder — failure path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recorder_exception_emits_event_with_error_field() -> None:
    with structlog.testing.capture_logs() as cap:
        stats = RunStats()
        with pytest.raises(RuntimeError, match="API timeout"):
            async with LLMCallRecorder(
                model=_MODEL,
                role=_ROLE,
                incident_id=_INCIDENT,
                run_stats=stats,
            ):
                raise RuntimeError("API timeout")

    assert len(cap) == 1
    event = cap[0]

    assert event["event"] == "llm.call"
    assert event["model"] == _MODEL
    assert event["role"] == _ROLE
    assert event["incident_id"] == _INCIDENT
    assert "error" in event
    assert "API timeout" in event["error"]
    assert "latency_ms" in event
    # Token counts must not appear on the error event (they were never set)
    assert "input_tokens" not in event
    assert "output_tokens" not in event


@pytest.mark.asyncio
async def test_recorder_exception_updates_run_stats_errors() -> None:
    stats = RunStats()
    with structlog.testing.capture_logs(), pytest.raises(ValueError):
        async with LLMCallRecorder(
            model=_MODEL,
            role=_ROLE,
            run_stats=stats,
        ):
            raise ValueError("bad response")

    assert stats.calls == 1
    assert stats.errors == 1
    assert stats.input_tokens == 0


@pytest.mark.asyncio
async def test_recorder_exception_reraises() -> None:
    """The context manager must NOT suppress exceptions."""
    with structlog.testing.capture_logs(), pytest.raises(RuntimeError):
        async with LLMCallRecorder(model=_MODEL, role=_ROLE):
            raise RuntimeError("must propagate")


# ---------------------------------------------------------------------------
# record_llm_call — functional convenience wrapper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_llm_call_context_manager() -> None:
    with structlog.testing.capture_logs() as cap:
        async with record_llm_call(
            model=_MODEL,
            role="Planner",
        ) as r:
            r.set_response(_make_message())

    assert len(cap) == 1
    assert cap[0]["event"] == "llm.call"
    assert cap[0]["role"] == "Planner"


# ---------------------------------------------------------------------------
# Pricing overridability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_custom_pricing_table_is_used() -> None:
    custom_pricing: dict[str, dict[str, float]] = {
        _MODEL: {
            "input": 0.0,
            "output": 0.0,
            "cache_write": 0.0,
            "cache_read": 0.0,
        }
    }
    with structlog.testing.capture_logs() as cap:
        async with LLMCallRecorder(
            model=_MODEL,
            role=_ROLE,
            pricing=custom_pricing,
        ) as r:
            r.set_response(_make_message(input_tokens=10_000, output_tokens=5_000))

    assert cap[0]["est_cost_usd"] == 0.0


# ---------------------------------------------------------------------------
# No incident_id — field present but None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recorder_without_incident_id() -> None:
    with structlog.testing.capture_logs() as cap:
        async with LLMCallRecorder(model=_MODEL, role=_ROLE) as r:
            r.set_response(_make_message())

    event = cap[0]
    assert event["incident_id"] is None
