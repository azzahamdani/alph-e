"""LLM client configuration — all tuneable knobs in one place."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LLMSettings:
    """Runtime settings for the Anthropic client wrapper.

    Defaults are appropriate for MVP1 (single-tier Claude Sonnet).
    """

    model: str = "claude-sonnet-4-6"
    timeout: float = 120.0
    max_retries: int = 2
    # Backoff: initial_backoff * (2 ** attempt). Values in seconds.
    initial_backoff: float = 1.0
    max_backoff: float = 30.0

    # Status codes that trigger a retry (429 + 5xx family).
    retryable_status_codes: frozenset[int] = field(
        default_factory=lambda: frozenset(
            {429, 500, 502, 503, 504}
        )
    )
