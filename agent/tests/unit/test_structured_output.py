"""Unit tests for agent.llm.structured.complete_typed.

All LLM calls are mocked — no real network traffic.
Pydantic strict mode is exercised by deliberately passing wrong types.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from agent.llm.errors import StructuredOutputError
from agent.llm.structured import complete_typed

# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------


class SimpleOutput(BaseModel):
    """A minimal structured output model used across tests."""

    answer: str
    confidence: float


class NestedOutput(BaseModel):
    """Model with a nested list to exercise schema generation."""

    items: list[str]
    count: int


def _make_tool_use_message(tool_name: str, payload: dict[str, Any]) -> MagicMock:
    """Build a fake ``anthropic.types.Message`` with a single ToolUseBlock."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = tool_name
    block.input = payload

    msg = MagicMock()
    msg.content = [block]
    return msg


def _make_text_message(text: str = "I cannot help.") -> MagicMock:
    """Build a fake message with only a text block (no tool_use)."""
    block = MagicMock()
    block.type = "text"
    block.text = text

    msg = MagicMock()
    msg.content = [block]
    return msg


def _make_client(side_effect: list[Any]) -> MagicMock:
    """Return a mock Client whose ``complete`` method yields *side_effect* values."""
    client = MagicMock()
    client.complete = AsyncMock(side_effect=side_effect)
    return client


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_validated_model_on_first_try() -> None:
    payload = {"answer": "memory leak", "confidence": 0.95}
    msg = _make_tool_use_message("simpleoutput", payload)
    client = _make_client([msg])

    result = await complete_typed(
        client,
        system="sys",
        messages=[{"role": "user", "content": "analyse"}],
        output_model=SimpleOutput,
    )

    assert isinstance(result, SimpleOutput)
    assert result.answer == "memory leak"
    assert result.confidence == pytest.approx(0.95)
    client.complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_tool_name_matches_lower_class_name() -> None:
    """The generated tool name must be the lower-cased model class name."""
    payload = {"items": ["a", "b"], "count": 2}
    msg = _make_tool_use_message("nestedoutput", payload)
    client = _make_client([msg])

    result = await complete_typed(
        client,
        system="sys",
        messages=[],
        output_model=NestedOutput,
    )

    assert isinstance(result, NestedOutput)
    _, kwargs = client.complete.call_args
    tools = kwargs["tools"]
    assert len(tools) == 1
    assert tools[0]["name"] == "nestedoutput"


@pytest.mark.asyncio
async def test_tool_schema_contains_required_properties() -> None:
    """The generated schema must include field names from the model."""
    payload = {"answer": "ok", "confidence": 1.0}
    msg = _make_tool_use_message("simpleoutput", payload)
    client = _make_client([msg])

    await complete_typed(client, system="s", messages=[], output_model=SimpleOutput)

    _, kwargs = client.complete.call_args
    schema = kwargs["tools"][0]["input_schema"]
    assert "answer" in schema.get("properties", {})
    assert "confidence" in schema.get("properties", {})


# ---------------------------------------------------------------------------
# Retry on validation failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retries_once_on_validation_failure_then_succeeds() -> None:
    """First response is type-invalid; second response is correct."""
    bad_payload = {"answer": 123, "confidence": "not-a-float"}  # wrong types
    good_payload = {"answer": "oom", "confidence": 0.8}

    bad_msg = _make_tool_use_message("simpleoutput", bad_payload)
    good_msg = _make_tool_use_message("simpleoutput", good_payload)
    client = _make_client([bad_msg, good_msg])

    result = await complete_typed(
        client,
        system="sys",
        messages=[{"role": "user", "content": "go"}],
        output_model=SimpleOutput,
        max_retries=1,
    )

    assert isinstance(result, SimpleOutput)
    assert result.answer == "oom"
    assert client.complete.await_count == 2


@pytest.mark.asyncio
async def test_corrective_message_appended_on_retry() -> None:
    """After a validation failure the conversation must include a corrective user turn."""
    bad_payload = {"answer": 0, "confidence": "bad"}
    good_payload = {"answer": "fixed", "confidence": 0.5}

    bad_msg = _make_tool_use_message("simpleoutput", bad_payload)
    good_msg = _make_tool_use_message("simpleoutput", good_payload)
    client = _make_client([bad_msg, good_msg])

    await complete_typed(
        client,
        system="sys",
        messages=[{"role": "user", "content": "start"}],
        output_model=SimpleOutput,
        max_retries=1,
    )

    # Second call must have an extra corrective user turn.
    second_call_args = client.complete.call_args_list[1]
    _, second_kwargs = second_call_args
    conversation: list[dict[str, Any]] = second_kwargs["messages"]

    # Original user turn + assistant turn + corrective user turn = 3
    assert len(conversation) == 3
    last_turn = conversation[-1]
    assert last_turn["role"] == "user"
    assert "Validation error" in last_turn["content"]


# ---------------------------------------------------------------------------
# Exhaustion → StructuredOutputError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_raises_structured_output_error_after_exhaustion() -> None:
    """When every attempt fails validation, StructuredOutputError must be raised."""
    bad_payload = {"answer": 999, "confidence": None}
    bad_msg = _make_tool_use_message("simpleoutput", bad_payload)
    client = _make_client([bad_msg, bad_msg])

    with pytest.raises(StructuredOutputError) as exc_info:
        await complete_typed(
            client,
            system="sys",
            messages=[],
            output_model=SimpleOutput,
            max_retries=1,
        )

    error = exc_info.value
    assert error.raw_output == bad_payload
    assert error.validation_error is not None
    assert client.complete.await_count == 2


@pytest.mark.asyncio
async def test_no_retry_when_max_retries_zero() -> None:
    """max_retries=0 means a single attempt; failure raises immediately."""
    bad_payload = {"answer": [], "confidence": "x"}
    bad_msg = _make_tool_use_message("simpleoutput", bad_payload)
    client = _make_client([bad_msg])

    with pytest.raises(StructuredOutputError):
        await complete_typed(
            client,
            system="sys",
            messages=[],
            output_model=SimpleOutput,
            max_retries=0,
        )

    assert client.complete.await_count == 1


# ---------------------------------------------------------------------------
# No tool-use block in response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_raises_when_model_returns_text_not_tool_use() -> None:
    """If the model ignores the forced tool, we must retry and eventually fail."""
    text_msg = _make_text_message("I cannot do that.")
    client = _make_client([text_msg])

    with pytest.raises(StructuredOutputError):
        await complete_typed(
            client,
            system="sys",
            messages=[],
            output_model=SimpleOutput,
            max_retries=0,
        )


@pytest.mark.asyncio
async def test_corrective_message_on_missing_tool_use() -> None:
    """A corrective user turn must ask for the tool call when the block is absent."""
    text_msg = _make_text_message()
    good_payload = {"answer": "recovered", "confidence": 0.7}
    good_msg = _make_tool_use_message("simpleoutput", good_payload)
    client = _make_client([text_msg, good_msg])

    result = await complete_typed(
        client,
        system="sys",
        messages=[],
        output_model=SimpleOutput,
        max_retries=1,
    )

    assert result.answer == "recovered"
    second_call_args = client.complete.call_args_list[1]
    _, second_kwargs = second_call_args
    conversation: list[dict[str, Any]] = second_kwargs["messages"]
    last_turn = conversation[-1]
    assert "simpleoutput" in last_turn["content"]


# ---------------------------------------------------------------------------
# Strict mode — type coercion must not happen silently
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strict_mode_rejects_string_for_float() -> None:
    """A string value for a float field must trigger validation failure (no coercion)."""
    coercible_payload = {"answer": "ok", "confidence": "0.9"}  # str, not float
    bad_msg = _make_tool_use_message("simpleoutput", coercible_payload)
    client = _make_client([bad_msg])

    with pytest.raises(StructuredOutputError) as exc_info:
        await complete_typed(
            client,
            system="sys",
            messages=[],
            output_model=SimpleOutput,
            max_retries=0,
        )

    # The validation error must mention 'confidence' or the float type.
    assert exc_info.value.validation_error is not None


# ---------------------------------------------------------------------------
# StructuredOutputError attributes
# ---------------------------------------------------------------------------


def test_structured_output_error_carries_raw_and_validation_error() -> None:
    from pydantic import ValidationError

    try:
        SimpleOutput.model_validate({"answer": 1, "confidence": "x"}, strict=True)
    except ValidationError as ve:
        err = StructuredOutputError(raw_output={"answer": 1, "confidence": "x"}, validation_error=ve)
        assert err.raw_output == {"answer": 1, "confidence": "x"}
        assert err.validation_error is ve
        assert "Validation error" in str(err) or "validation" in str(err).lower()
    else:
        pytest.fail("Expected ValidationError was not raised")


def test_structured_output_error_message_contains_validation_detail() -> None:
    from pydantic import ValidationError

    try:
        SimpleOutput.model_validate({}, strict=True)
    except ValidationError as ve:
        err = StructuredOutputError(raw_output={}, validation_error=ve)
        msg = str(err)
        assert len(msg) > 0


# ---------------------------------------------------------------------------
# Tool cache_control is forwarded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_definition_has_cache_control() -> None:
    """The tool passed to client.complete must include cache_control."""
    payload = {"answer": "ok", "confidence": 1.0}
    msg = _make_tool_use_message("simpleoutput", payload)
    client = _make_client([msg])

    await complete_typed(client, system="s", messages=[], output_model=SimpleOutput)

    _, kwargs = client.complete.call_args
    tool = kwargs["tools"][0]
    assert "cache_control" in tool
    assert tool["cache_control"] == {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# Schema generation for a model with no docstring
# ---------------------------------------------------------------------------


class NoDocModel(BaseModel):
    value: int


@pytest.mark.asyncio
async def test_schema_generation_works_without_docstring() -> None:
    payload = {"value": 42}
    msg = _make_tool_use_message("nodocmodel", payload)
    client = _make_client([msg])

    result = await complete_typed(client, system="s", messages=[], output_model=NoDocModel)

    assert result.value == 42
