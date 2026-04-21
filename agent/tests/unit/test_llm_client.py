"""Unit tests for agent.llm.Client.

All Anthropic SDK calls are mocked — no real network traffic.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import httpx
import pytest

from agent.llm import Client, LLMSettings, MissingAPIKeyError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_usage(
    input_tokens: int = 10,
    output_tokens: int = 20,
    cache_creation: int = 0,
    cache_read: int = 0,
) -> MagicMock:
    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    usage.cache_creation_input_tokens = cache_creation
    usage.cache_read_input_tokens = cache_read
    return usage


def _make_message(content: str = "Hello") -> MagicMock:
    msg = MagicMock()
    msg.model = "claude-sonnet-4-6"
    msg.usage = _make_usage()
    block = MagicMock()
    block.text = content
    msg.content = [block]
    return msg


def _make_api_error(status_code: int) -> anthropic.APIStatusError:
    response = httpx.Response(status_code, request=httpx.Request("POST", "https://api.anthropic.com"))
    return anthropic.APIStatusError(
        message=f"HTTP {status_code}",
        response=response,
        body=None,
    )


# ---------------------------------------------------------------------------
# MissingAPIKeyError
# ---------------------------------------------------------------------------


def test_missing_api_key_raises_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(MissingAPIKeyError):
        Client()


def test_missing_api_key_error_message() -> None:
    with pytest.raises(MissingAPIKeyError, match="ANTHROPIC_API_KEY"):
        Client(api_key="")  # empty string → falsy → raises


def test_api_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-env")
    with patch("anthropic.AsyncAnthropic"):
        client = Client()
    assert client is not None


def test_api_key_explicit_kwarg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with patch("anthropic.AsyncAnthropic"):
        client = Client(api_key="sk-explicit")
    assert client is not None


# ---------------------------------------------------------------------------
# Successful call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_returns_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    expected = _make_message("answer")

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        mock_instance = mock_cls.return_value
        mock_instance.messages.create = AsyncMock(return_value=expected)

        client = Client(api_key="sk-test")
        result = await client.complete(
            system="You are a helpful assistant.",
            messages=[{"role": "user", "content": "Hello"}],
        )

    assert result is expected


@pytest.mark.asyncio
async def test_complete_passes_max_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    expected = _make_message()

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        mock_instance = mock_cls.return_value
        mock_instance.messages.create = AsyncMock(return_value=expected)

        client = Client(api_key="sk-test")
        await client.complete(
            system="sys",
            messages=[],
            max_tokens=512,
        )
        _, kwargs = mock_instance.messages.create.call_args
        assert kwargs["max_tokens"] == 512


@pytest.mark.asyncio
async def test_complete_passes_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    tools: list[dict[str, Any]] = [{"name": "my_tool", "description": "does stuff"}]
    expected = _make_message()

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        mock_instance = mock_cls.return_value
        mock_instance.messages.create = AsyncMock(return_value=expected)

        client = Client(api_key="sk-test")
        await client.complete(system="sys", messages=[], tools=tools)
        _, kwargs = mock_instance.messages.create.call_args
        assert kwargs["tools"] == tools


@pytest.mark.asyncio
async def test_no_tools_key_when_tools_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    expected = _make_message()

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        mock_instance = mock_cls.return_value
        mock_instance.messages.create = AsyncMock(return_value=expected)

        client = Client(api_key="sk-test")
        await client.complete(system="sys", messages=[], tools=None)
        _, kwargs = mock_instance.messages.create.call_args
        assert "tools" not in kwargs


# ---------------------------------------------------------------------------
# Cache control applied to system message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_control_applied_to_system_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    expected = _make_message()

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        mock_instance = mock_cls.return_value
        mock_instance.messages.create = AsyncMock(return_value=expected)

        client = Client(api_key="sk-test")
        await client.complete(system="Be precise.", messages=[])
        _, kwargs = mock_instance.messages.create.call_args

    system_blocks = kwargs["system"]
    assert len(system_blocks) == 1
    block = system_blocks[0]
    assert block["type"] == "text"
    assert block["text"] == "Be precise."
    assert block["cache_control"] == {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retries_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    error = _make_api_error(429)
    expected = _make_message()

    with patch("anthropic.AsyncAnthropic") as mock_cls, patch("asyncio.sleep", new_callable=AsyncMock):
        mock_instance = mock_cls.return_value
        mock_instance.messages.create = AsyncMock(
            side_effect=[error, expected]
        )

        settings = LLMSettings(max_retries=2, initial_backoff=0.0)
        client = Client(api_key="sk-test", settings=settings)
        result = await client.complete(system="sys", messages=[])

    assert result is expected
    assert mock_instance.messages.create.call_count == 2


@pytest.mark.asyncio
async def test_retries_on_500(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    error = _make_api_error(500)
    expected = _make_message()

    with patch("anthropic.AsyncAnthropic") as mock_cls, patch("asyncio.sleep", new_callable=AsyncMock):
        mock_instance = mock_cls.return_value
        mock_instance.messages.create = AsyncMock(
            side_effect=[error, expected]
        )

        settings = LLMSettings(max_retries=2, initial_backoff=0.0)
        client = Client(api_key="sk-test", settings=settings)
        result = await client.complete(system="sys", messages=[])

    assert result is expected


@pytest.mark.asyncio
async def test_does_not_retry_on_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only 429 is retryable; other 4xx errors must surface immediately."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import anthropic as ant
    error = _make_api_error(400)

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        mock_instance = mock_cls.return_value
        mock_instance.messages.create = AsyncMock(side_effect=error)

        settings = LLMSettings(max_retries=2)
        client = Client(api_key="sk-test", settings=settings)

        with pytest.raises(ant.APIStatusError) as exc_info:
            await client.complete(system="sys", messages=[])

    assert exc_info.value.status_code == 400
    assert mock_instance.messages.create.call_count == 1


@pytest.mark.asyncio
async def test_does_not_retry_on_401(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import anthropic as ant
    error = _make_api_error(401)

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        mock_instance = mock_cls.return_value
        mock_instance.messages.create = AsyncMock(side_effect=error)

        client = Client(api_key="sk-test")

        with pytest.raises(ant.APIStatusError) as exc_info:
            await client.complete(system="sys", messages=[])

    assert exc_info.value.status_code == 401
    assert mock_instance.messages.create.call_count == 1


@pytest.mark.asyncio
async def test_raises_after_max_retries_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import anthropic as ant
    error = _make_api_error(503)

    with patch("anthropic.AsyncAnthropic") as mock_cls, patch("asyncio.sleep", new_callable=AsyncMock):
        mock_instance = mock_cls.return_value
        # Always fail
        mock_instance.messages.create = AsyncMock(side_effect=error)

        settings = LLMSettings(max_retries=2, initial_backoff=0.0)
        client = Client(api_key="sk-test", settings=settings)

        with pytest.raises(ant.APIStatusError):
            await client.complete(system="sys", messages=[])

    # Called once initially + 2 retries = 3 total
    assert mock_instance.messages.create.call_count == 3


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_default_model_is_sonnet() -> None:
    settings = LLMSettings()
    assert settings.model == "claude-sonnet-4-6"


def test_retryable_status_codes_include_429_and_5xx() -> None:
    settings = LLMSettings()
    assert 429 in settings.retryable_status_codes
    assert 500 in settings.retryable_status_codes
    assert 502 in settings.retryable_status_codes
    assert 503 in settings.retryable_status_codes
    assert 504 in settings.retryable_status_codes


def test_retryable_status_codes_exclude_4xx() -> None:
    settings = LLMSettings()
    for code in (400, 401, 403, 404, 422):
        assert code not in settings.retryable_status_codes
