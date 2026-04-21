"""LLM-call observability — structured logging and cost estimation.

Every LLM call wrapped by :class:`LLMCallRecorder` emits a single
``structlog`` event of type ``"llm.call"`` carrying model, role, token counts,
latency, and an informational cost estimate.  Message contents are never
logged.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import structlog
from anthropic.types import Message

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Pricing table — USD per 1 M tokens.
# Overridable at runtime: LLMCallRecorder(pricing=my_table).
# Source: Anthropic pricing as of 2024.
# ---------------------------------------------------------------------------

DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {
        "input": 3.00,
        "output": 15.00,
        "cache_write": 3.75,
        "cache_read": 0.30,
    },
    # Aliases / common short names that may appear in Message.model
    "claude-3-5-sonnet-20241022": {
        "input": 3.00,
        "output": 15.00,
        "cache_write": 3.75,
        "cache_read": 0.30,
    },
    "claude-3-5-haiku-20241022": {
        "input": 0.80,
        "output": 4.00,
        "cache_write": 1.00,
        "cache_read": 0.08,
    },
}

_PER_M = 1_000_000.0


def _estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int,
    cache_read_tokens: int,
    pricing: dict[str, dict[str, float]],
) -> float:
    """Return an informational USD cost estimate; 0.0 if model is unknown."""
    rates = pricing.get(model)
    if rates is None:
        return 0.0
    cost = (
        input_tokens * rates["input"] / _PER_M
        + output_tokens * rates["output"] / _PER_M
        + cache_creation_tokens * rates["cache_write"] / _PER_M
        + cache_read_tokens * rates["cache_read"] / _PER_M
    )
    return round(cost, 8)


# ---------------------------------------------------------------------------
# RunStats — lightweight in-memory aggregator for tests / eval harness
# ---------------------------------------------------------------------------


@dataclass
class RunStats:
    """Accumulated token counts and cost for a single agent run."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    est_cost_usd: float = 0.0
    errors: int = 0

    def record_success(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_creation_tokens: int,
        cache_read_tokens: int,
        est_cost_usd: float,
    ) -> None:
        self.calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_creation_tokens += cache_creation_tokens
        self.cache_read_tokens += cache_read_tokens
        self.est_cost_usd += est_cost_usd

    def record_error(self) -> None:
        self.calls += 1
        self.errors += 1


# ---------------------------------------------------------------------------
# LLMCallRecorder — async context manager
# ---------------------------------------------------------------------------


class LLMCallRecorder:
    """Async context manager that wraps an ``anthropic.types.Message``-returning
    coroutine and emits a structured ``"llm.call"`` log event on exit.

    Usage::

        recorder = LLMCallRecorder(model="claude-sonnet-4-6", role="Investigator")
        async with recorder as r:
            message = await client.messages.create(...)
            r.set_response(message)

    The context manager catches *all* exceptions, emits an event with
    ``error`` set, then re-raises so callers are unaffected.
    """

    def __init__(
        self,
        *,
        model: str,
        role: str,
        incident_id: str | None = None,
        pricing: dict[str, dict[str, float]] | None = None,
        run_stats: RunStats | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.model = model
        self.role = role
        self.incident_id = incident_id
        self.pricing = pricing if pricing is not None else DEFAULT_PRICING
        self.run_stats = run_stats
        self.extra: dict[str, Any] = extra or {}

        self._response: Message | None = None
        self._start: float = 0.0

    # ------------------------------------------------------------------
    # Public API used inside the ``async with`` block
    # ------------------------------------------------------------------

    def set_response(self, message: Message) -> None:
        """Record the successful API response for emission on ``__aexit__``."""
        self._response = message

    # ------------------------------------------------------------------
    # Context manager protocol
    # ------------------------------------------------------------------

    async def __aenter__(self) -> LLMCallRecorder:
        self._start = time.monotonic()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> bool:
        latency_ms = round((time.monotonic() - self._start) * 1000, 2)

        if exc_type is not None:
            # Failure path — emit with error field, update stats, re-raise.
            log.error(
                "llm.call",
                model=self.model,
                role=self.role,
                incident_id=self.incident_id,
                latency_ms=latency_ms,
                error=str(exc_val),
                **self.extra,
            )
            if self.run_stats is not None:
                self.run_stats.record_error()
            return False  # do not suppress the exception

        # Success path
        msg = self._response
        input_tokens: int = 0
        output_tokens: int = 0
        cache_creation_tokens: int = 0
        cache_read_tokens: int = 0

        if msg is not None:
            usage = msg.usage
            input_tokens = usage.input_tokens
            output_tokens = usage.output_tokens
            cache_creation_tokens = getattr(usage, "cache_creation_input_tokens", 0) or 0
            cache_read_tokens = getattr(usage, "cache_read_input_tokens", 0) or 0

        est_cost_usd = _estimate_cost(
            model=self.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_tokens=cache_creation_tokens,
            cache_read_tokens=cache_read_tokens,
            pricing=self.pricing,
        )

        log.info(
            "llm.call",
            model=self.model,
            role=self.role,
            incident_id=self.incident_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_tokens=cache_creation_tokens,
            cache_read_tokens=cache_read_tokens,
            latency_ms=latency_ms,
            est_cost_usd=est_cost_usd,
            **self.extra,
        )

        if self.run_stats is not None:
            self.run_stats.record_success(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_creation_tokens=cache_creation_tokens,
                cache_read_tokens=cache_read_tokens,
                est_cost_usd=est_cost_usd,
            )

        return False


# ---------------------------------------------------------------------------
# Convenience helper — functional style
# ---------------------------------------------------------------------------


@asynccontextmanager
async def record_llm_call(
    *,
    model: str,
    role: str,
    incident_id: str | None = None,
    pricing: dict[str, dict[str, float]] | None = None,
    run_stats: RunStats | None = None,
    extra: dict[str, Any] | None = None,
) -> AsyncIterator[LLMCallRecorder]:
    """Async context manager — thin wrapper around :class:`LLMCallRecorder`.

    Example::

        async with record_llm_call(model="claude-sonnet-4-6", role="Planner") as r:
            msg = await client.messages.create(...)
            r.set_response(msg)
    """
    recorder = LLMCallRecorder(
        model=model,
        role=role,
        incident_id=incident_id,
        pricing=pricing,
        run_stats=run_stats,
        extra=extra,
    )
    async with recorder as r:
        yield r
