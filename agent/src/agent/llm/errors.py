"""Typed exception hierarchy for the LLM subsystem.

``MissingAPIKeyError`` is defined here as the canonical location; ``client.py``
re-exports it for backwards compatibility.
"""

from __future__ import annotations

from pydantic import ValidationError


class LLMError(RuntimeError):
    """Base class for all LLM-subsystem errors."""


class MissingAPIKeyError(LLMError):
    """Raised when ``ANTHROPIC_API_KEY`` is absent from the environment."""

    def __init__(self) -> None:
        super().__init__(
            "ANTHROPIC_API_KEY is not set. "
            "Export the environment variable before starting the agent."
        )


class StructuredOutputError(LLMError):
    """Raised when ``complete_typed`` exhausts all retries without a valid parse.

    Attributes
    ----------
    raw_output:
        The last raw string or object returned by the model before giving up.
    validation_error:
        The :class:`pydantic.ValidationError` from the final parse attempt.
    """

    def __init__(self, raw_output: object, validation_error: ValidationError) -> None:
        super().__init__(
            f"Structured output validation failed after all retries. "
            f"Last validation error: {validation_error}"
        )
        self.raw_output = raw_output
        self.validation_error = validation_error
