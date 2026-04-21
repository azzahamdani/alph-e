"""Unified-diff syntactic validator using the ``unidiff`` library.

The Dev agent calls :func:`validate_file_changes` before returning a
``FixProposal``.  If any ``FileChange.diff`` fails to parse as a valid unified
diff the function raises :class:`DiffValidationError` carrying all failure
details so the caller can issue a corrective retry.

Design constraints:
- Never writes to disk — validation is pure in-memory parsing only.
- Never applies the patch — ``git apply --check`` is the caller's
  responsibility (tests use a known repo fixture for that).
- One error per file, collected eagerly so the corrective prompt is complete.
"""

from __future__ import annotations

import unidiff  # type: ignore[import-untyped]

from agent.schemas.remediation import FileChange


class DiffValidationError(ValueError):
    """Raised when one or more diffs in a ``FixProposal`` fail to parse.

    Attributes
    ----------
    failures:
        Mapping of ``(repo, path)`` to the parse-error detail string.
    """

    def __init__(self, failures: dict[tuple[str, str], str]) -> None:
        lines = "\n".join(
            f"  {repo}/{path}: {detail}" for (repo, path), detail in failures.items()
        )
        super().__init__(f"Diff validation failed for {len(failures)} file(s):\n{lines}")
        self.failures = failures


def validate_file_changes(changes: list[FileChange]) -> None:
    """Raise :class:`DiffValidationError` if any change carries an invalid diff.

    Parameters
    ----------
    changes:
        The ``FixProposal.changes`` list produced by the LLM.

    Raises
    ------
    DiffValidationError
        If one or more diffs cannot be parsed as valid unified diffs.
    ValueError
        If ``changes`` is empty (a proposal with no changes is always invalid).
    """
    if not changes:
        raise ValueError("FixProposal.changes must contain at least one FileChange.")

    failures: dict[tuple[str, str], str] = {}

    for fc in changes:
        key = (fc.repo, fc.path)
        diff_text = fc.diff.strip()

        if not diff_text:
            failures[key] = "diff is empty"
            continue

        try:
            patch_set = unidiff.PatchSet(diff_text)
        except unidiff.UnidiffParseError as exc:
            failures[key] = str(exc)
            continue

        if len(patch_set) == 0:
            failures[key] = "diff parsed but contains no hunks"
            continue

    if failures:
        raise DiffValidationError(failures)


def is_valid_diff(diff_text: str) -> bool:
    """Return ``True`` if *diff_text* is a syntactically valid unified diff.

    Convenience helper for callers that want a bool rather than an exception.
    """
    if not diff_text.strip():
        return False
    try:
        patch_set = unidiff.PatchSet(diff_text.strip())
        return len(patch_set) > 0
    except unidiff.UnidiffParseError:
        return False
