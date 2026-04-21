"""Anthropic LLM client wrapper with prompt caching and retry logic.

All reasoning nodes import this module — no direct ``anthropic.Anthropic()``
calls are allowed elsewhere in the codebase.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import anthropic
import structlog
from anthropic.types import Message, TextBlockParam

from agent.llm.settings import LLMSettings

log = structlog.get_logger(__name__)


class MissingAPIKeyError(RuntimeError):
    """Raised when ``ANTHROPIC_API_KEY`` is absent from the environment."""

    def __init__(self) -> None:
        super().__init__(
            "ANTHROPIC_API_KEY is not set. "
            "Export the environment variable before starting the agent."
        )


class Client:
    """Thin async wrapper around the Anthropic Messages API.

    Features
    --------
    - Applies ``cache_control: {"type": "ephemeral"}`` to the system message
      so the stable system prompt benefits from prompt caching.
    - Retries on 429 / 5xx with exponential backoff (max 2 retries).
    - Logs model, token counts, cache hit stats, and call latency via structlog.
    """

    def __init__(
        self,
        settings: LLMSettings | None = None,
        *,
        api_key: str | None = None,
    ) -> None:
        self._settings = settings or LLMSettings()
        resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not resolved_key:
            raise MissingAPIKeyError()
        self._anthropic = anthropic.AsyncAnthropic(
            api_key=resolved_key,
            timeout=self._settings.timeout,
        )

    async def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
    ) -> Message:
        """Call the Anthropic Messages API and return the parsed ``Message``.

        Parameters
        ----------
        system:
            The system prompt text. ``cache_control`` is applied automatically.
        messages:
            Conversation turns in Anthropic's ``MessageParam`` format.
        tools:
            Optional tool definitions. If supplied they are forwarded verbatim
            (callers may embed ``cache_control`` themselves).
        max_tokens:
            Upper bound on the response length; defaults to 4096.

        Returns
        -------
        :class:`anthropic.types.Message`
            The full, parsed response object (including usage statistics).

        Raises
        ------
        MissingAPIKeyError
            Propagated from ``__init__`` if the key was absent.
        anthropic.APIStatusError
            Re-raised after all retries are exhausted.
        """
        system_block: TextBlockParam = {
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }

        attempt = 0
        last_exc: anthropic.APIStatusError | None = None

        while True:
            t0 = time.monotonic()
            try:
                kwargs: dict[str, Any] = {
                    "model": self._settings.model,
                    "max_tokens": max_tokens,
                    "system": [system_block],
                    "messages": messages,
                }
                if tools is not None:
                    kwargs["tools"] = tools

                response: Message = await self._anthropic.messages.create(**kwargs)

            except anthropic.APIStatusError as exc:
                last_exc = exc
                if exc.status_code not in self._settings.retryable_status_codes:
                    log.warning(
                        "llm.non_retryable_error",
                        status_code=exc.status_code,
                        attempt=attempt,
                    )
                    raise

                if attempt >= self._settings.max_retries:
                    log.error(
                        "llm.retries_exhausted",
                        status_code=exc.status_code,
                        attempts=attempt + 1,
                    )
                    raise

                backoff = min(
                    self._settings.initial_backoff * (2**attempt),
                    self._settings.max_backoff,
                )
                log.warning(
                    "llm.retrying",
                    status_code=exc.status_code,
                    attempt=attempt,
                    backoff_seconds=backoff,
                )
                await asyncio.sleep(backoff)
                attempt += 1
                continue

            latency_ms = (time.monotonic() - t0) * 1000
            usage = response.usage
            cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
            cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0

            log.info(
                "llm.complete",
                model=response.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_creation_tokens=cache_creation,
                cache_read_tokens=cache_read,
                latency_ms=round(latency_ms, 1),
                attempt=attempt,
            )
            return response

        # Unreachable but satisfies mypy's exhaustion check.
        assert last_exc is not None  # noqa: S101
        raise last_exc
