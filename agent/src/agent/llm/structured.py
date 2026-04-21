"""Structured-output helper: Pydantic model extraction via Anthropic tool-use.

Usage
-----
.. code-block:: python

    result = await complete_typed(
        client,
        system="You are a DevOps analyst.",
        messages=[{"role": "user", "content": "Analyse this alert."}],
        output_model=MyOutputModel,
    )

The helper converts ``output_model`` to an Anthropic tool definition using
Pydantic's ``model_json_schema()``, calls the model with ``tool_choice`` set to
force that single tool, and validates the returned JSON.

On ``ValidationError`` it appends a corrective user turn describing the error
and retries up to ``max_retries`` times. After exhaustion it raises
:class:`~agent.llm.errors.StructuredOutputError`.

Logging policy
--------------
- Raw model output is logged at **DEBUG** only — never at INFO.
- Retry events are logged at WARNING.
- Success is logged at DEBUG.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from pydantic import BaseModel, ValidationError

from agent.llm.client import Client
from agent.llm.errors import StructuredOutputError

log = structlog.get_logger(__name__)

# Internal sentinel used to detect an absent tool-use block in the response.
_SENTINEL = object()


def _build_tool_definition(output_model: type[BaseModel]) -> dict[str, Any]:
    """Return an Anthropic tool definition derived from *output_model*.

    The tool name is the lower-cased model class name so it is stable and
    deterministic across calls.
    """
    schema = output_model.model_json_schema()
    return {
        "name": output_model.__name__.lower(),
        "description": (
            output_model.__doc__.strip()
            if output_model.__doc__
            else f"Structured output for {output_model.__name__}"
        ),
        "input_schema": schema,
    }


def _extract_tool_input(message: Any, tool_name: str) -> object:
    """Pull the ``input`` dict from the first matching ``tool_use`` block.

    Returns ``_SENTINEL`` when no matching block is found.
    """
    for block in message.content:
        if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
            return block.input
    return _SENTINEL


async def complete_typed[T: BaseModel](
    client: Client,
    *,
    system: str,
    messages: list[dict[str, Any]],
    output_model: type[T],
    max_retries: int = 1,
) -> T:
    """Call the LLM and parse the response into *output_model*.

    Parameters
    ----------
    client:
        Initialised :class:`~agent.llm.client.Client` instance.
    system:
        System prompt text (passed straight through to ``client.complete``).
    messages:
        Conversation turns in Anthropic ``MessageParam`` format.
    output_model:
        A :class:`~pydantic.BaseModel` subclass.  The JSON schema is generated
        automatically — no hand-written schemas at call sites.
    max_retries:
        Maximum number of *additional* attempts after the first validation
        failure.  ``0`` means one attempt total (no retry).

    Returns
    -------
    T
        A validated instance of *output_model* using **strict** Pydantic mode.

    Raises
    ------
    StructuredOutputError
        When all retries are exhausted without a valid parse.  The exception
        carries ``raw_output`` and ``validation_error`` attributes.
    """
    tool_def = _build_tool_definition(output_model)
    tool_name = tool_def["name"]

    # Work on a mutable copy so we can append corrective turns without mutating
    # the caller's list.
    conversation: list[dict[str, Any]] = list(messages)

    last_raw: object = None
    last_exc: ValidationError | None = None

    for attempt in range(max_retries + 1):
        message = await client.complete(
            system=system,
            messages=conversation,
            tools=[{**tool_def, "cache_control": {"type": "ephemeral"}}],
        )

        raw_input = _extract_tool_input(message, tool_name)

        if raw_input is _SENTINEL:
            # Model did not call the tool — treat this as a validation failure
            # by building a synthetic error payload so the corrective branch
            # below handles it uniformly.
            missing_payload: dict[str, Any] = {}
            corrective_detail = (
                f"You did not call the '{tool_name}' tool. "
                "You MUST respond by calling that tool with a valid JSON payload."
            )
            log.debug(
                "structured_output.no_tool_use",
                attempt=attempt,
                tool_name=tool_name,
            )
            # Build a fake raw value so StructuredOutputError is informative.
            last_raw = missing_payload
            # We have to produce a ValidationError; use a minimal model parse.
            try:
                output_model.model_validate({}, strict=True)
            except ValidationError as ve:
                last_exc = ve
            # Fall through to the corrective-message logic below.
        else:
            log.debug(
                "structured_output.raw_response",
                attempt=attempt,
                tool_name=tool_name,
                raw=raw_input,
            )
            last_raw = raw_input
            try:
                validated = output_model.model_validate(raw_input, strict=True)
                log.debug(
                    "structured_output.success",
                    attempt=attempt,
                    tool_name=tool_name,
                )
                return validated
            except ValidationError as exc:
                last_exc = exc
                corrective_detail = str(exc)
                log.warning(
                    "structured_output.validation_failure",
                    attempt=attempt,
                    max_retries=max_retries,
                    tool_name=tool_name,
                )

        if attempt >= max_retries:
            # Exhausted — hard failure as required by the spec.
            break

        # Append the assistant turn (the bad response) and a corrective user
        # turn so the model understands what went wrong.
        raw_json = json.dumps(raw_input) if raw_input is not _SENTINEL else "{}"
        conversation.append(
            {
                "role": "assistant",
                "content": message.content,
            }
        )
        conversation.append(
            {
                "role": "user",
                "content": (
                    f"Your previous response did not satisfy the required schema. "
                    f"Validation error:\n\n{corrective_detail}\n\n"
                    f"Raw output received:\n\n{raw_json}\n\n"
                    f"Please call the '{tool_name}' tool again with a corrected, "
                    "fully valid JSON payload."
                ),
            }
        )

    # If we reach here, all attempts failed.
    assert last_exc is not None, "last_exc must be set before exhaustion"  # noqa: S101
    log.debug(
        "structured_output.exhausted",
        tool_name=tool_name,
        attempts=max_retries + 1,
    )
    raise StructuredOutputError(raw_output=last_raw, validation_error=last_exc)
