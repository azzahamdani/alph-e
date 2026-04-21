"""Unit tests for agent.prompts loader.

All tests are pure in-memory — no LLM calls, no disk mutations.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.prompts import PromptBundle, PromptNotFoundError, load
from agent.prompts.loader import _system_cache_key, _system_prefix

_PROMPTS_DIR = Path(__file__).parent.parent.parent / "src" / "agent" / "prompts"

_REQUIRED_ROLES = [
    "investigator",
    "planner",
    "dev",
    "verifier",
    "reviewer",
    "coordinator",
]


@pytest.mark.unit
@pytest.mark.parametrize("role", _REQUIRED_ROLES)
def test_load_required_role(role: str) -> None:
    bundle = load(role)
    assert isinstance(bundle, PromptBundle)
    assert bundle.system_prefix
    assert bundle.role_prompt
    assert bundle.cache_key


@pytest.mark.unit
@pytest.mark.parametrize("role", _REQUIRED_ROLES)
def test_bundle_contains_system_prefix(role: str) -> None:
    bundle = load(role)
    assert bundle.system_prefix == _system_prefix


@pytest.mark.unit
@pytest.mark.parametrize("role", _REQUIRED_ROLES)
def test_bundle_role_prompt_matches_file(role: str) -> None:
    expected = (_PROMPTS_DIR / f"{role}.md").read_text(encoding="utf-8")
    bundle = load(role)
    assert bundle.role_prompt == expected


@pytest.mark.unit
def test_cache_key_is_sha256_of_system_prefix() -> None:
    expected = hashlib.sha256(_system_prefix.encode()).hexdigest()
    assert _system_cache_key == expected


@pytest.mark.unit
def test_cache_key_stable_across_calls() -> None:
    b1 = load("investigator")
    b2 = load("planner")
    assert b1.cache_key == b2.cache_key


@pytest.mark.unit
def test_cache_key_length() -> None:
    bundle = load("investigator")
    assert len(bundle.cache_key) == 64


@pytest.mark.unit
def test_prompt_bundle_is_frozen() -> None:
    bundle = load("investigator")
    with pytest.raises((AttributeError, TypeError)):
        bundle.system_prefix = "mutated"  # type: ignore[misc]


@pytest.mark.unit
def test_missing_role_raises_prompt_not_found_error() -> None:
    with pytest.raises(PromptNotFoundError) as exc_info:
        load("nonexistent_role_xyz")
    assert exc_info.value.role == "nonexistent_role_xyz"


@pytest.mark.unit
def test_prompt_not_found_error_is_file_not_found() -> None:
    with pytest.raises(FileNotFoundError):
        load("another_missing_role")


@pytest.mark.unit
def test_intake_role_absence_is_tolerated() -> None:
    """intake.md may or may not exist — if absent, load() raises PromptNotFoundError cleanly."""
    intake_path = _PROMPTS_DIR / "intake.md"
    if intake_path.exists():
        bundle = load("intake")
        assert bundle.role_prompt
    else:
        with pytest.raises(PromptNotFoundError):
            load("intake")


@pytest.mark.unit
def test_repeated_calls_return_same_bundle_content() -> None:
    b1 = load("investigator")
    b2 = load("investigator")
    assert b1 == b2


@pytest.mark.unit
def test_load_does_not_open_files_on_hot_path() -> None:
    """load() must not call open() after the module is already imported."""
    with patch("builtins.open") as mock_open:
        _ = load("investigator")
        mock_open.assert_not_called()
