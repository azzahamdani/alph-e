"""Prompt loader — reads role prompts from the prompts directory.

Files are read once at import time and never again on the hot path.
The system.md prefix is the cache-stable portion passed to Anthropic with
``cache_control`` set; ``cache_key`` is a SHA-256 of that text for
observability.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent


class PromptNotFoundError(FileNotFoundError):
    """Raised when a requested role prompt file does not exist."""

    def __init__(self, role: str) -> None:
        self.role = role
        super().__init__(f"No prompt file found for role '{role}' (expected {role}.md)")


@dataclass(frozen=True)
class PromptBundle:
    """Immutable bundle of the system prefix and a role-specific prompt."""

    system_prefix: str
    """Shared system.md content — this is the cache-stable portion."""

    role_prompt: str
    """Role-specific prompt content."""

    cache_key: str
    """Stable SHA-256 hex digest of ``system_prefix`` for cache observability."""


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Module-level cache — populated exactly once at import time.
# ---------------------------------------------------------------------------

_system_prefix: str = _read(_PROMPTS_DIR / "system.md")
_system_cache_key: str = hashlib.sha256(_system_prefix.encode()).hexdigest()

# Pre-load every *.md file that exists (except system.md) into a dict so that
# `load()` is a pure in-memory lookup with no I/O on the hot path.
_role_cache: dict[str, str] = {
    path.stem: _read(path) for path in _PROMPTS_DIR.glob("*.md") if path.stem != "system"
}


def load(role: str) -> PromptBundle:
    """Return a :class:`PromptBundle` for *role*.

    Parameters
    ----------
    role:
        Bare role name without the ``.md`` extension (e.g. ``"investigator"``).

    Raises
    ------
    PromptNotFoundError
        If no ``<role>.md`` file was present when the module was first imported.
    """
    try:
        role_prompt = _role_cache[role]
    except KeyError:
        raise PromptNotFoundError(role) from None

    return PromptBundle(
        system_prefix=_system_prefix,
        role_prompt=role_prompt,
        cache_key=_system_cache_key,
    )
