"""agent.prompts — prompt loading utilities.

Public API
----------
load(role)       → PromptBundle
PromptBundle     — frozen dataclass (system_prefix, role_prompt, cache_key)
PromptNotFoundError — raised when a role file is absent
"""

from agent.prompts.loader import PromptBundle, PromptNotFoundError, load

__all__ = ["PromptBundle", "PromptNotFoundError", "load"]
