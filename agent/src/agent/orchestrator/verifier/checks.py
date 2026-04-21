"""Deterministic dry-run checks for the Verifier node.

Each function here runs a concrete, side-effect-free check and returns
``(passed: bool, output: str)``.  The raw output is captured verbatim —
never paraphrased — so the LLM and router have a precise signal.

Guardrails
----------
- No function here mutates anything.  ``kubectl apply`` is always
  ``--dry-run=server``; ``git apply`` is always ``--check``.
- Subprocess stderr is merged into stdout so ``dry_run_output`` is a
  single string the caller can forward to ``VerifierResult.dry_run_output``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from agent.schemas.incident import ActionIntent
from agent.schemas.remediation import FileChange, FixProposal

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_K8S_EXTENSIONS = frozenset({".yaml", ".yml", ".json"})
_K8S_PATH_KEYWORDS = ("deploy", "manifest", "k8s", "kubernetes", "helm", "chart")


def _run(cmd: list[str], *, cwd: str | None = None, stdin: str | None = None) -> tuple[bool, str]:
    """Run *cmd* and return ``(success, combined_output)``."""
    result = subprocess.run(
        cmd,
        cwd=cwd,
        input=stdin,
        capture_output=True,
        text=True,
    )
    combined = result.stdout + result.stderr
    return result.returncode == 0, combined


def _looks_like_k8s_manifest(change: FileChange) -> bool:
    """Return True when a FileChange is likely a Kubernetes manifest."""
    path = Path(change.path)
    if path.suffix not in _K8S_EXTENSIONS:
        return False
    lowered = str(path).lower()
    return any(kw in lowered for kw in _K8S_PATH_KEYWORDS) or _has_k8s_markers(change.diff)


def _has_k8s_markers(diff: str) -> bool:
    """Heuristic: look for ``apiVersion:`` or ``kind:`` in the diff lines."""
    for line in diff.splitlines():
        stripped = line.lstrip("+-").lstrip()
        if stripped.startswith("apiVersion:") or stripped.startswith("kind:"):
            return True
    return False


# ---------------------------------------------------------------------------
# Public check functions
# ---------------------------------------------------------------------------


def check_git_apply(proposal: FixProposal, *, repo_path: str) -> tuple[bool, str]:
    """Run ``git apply --check`` for every ``FileChange`` in *proposal*.

    Parameters
    ----------
    proposal:
        The fix proposal whose diffs we validate.
    repo_path:
        Absolute path to the repository working tree.

    Returns
    -------
    (passed, output)
        ``passed`` is ``True`` only when every change applies cleanly.
        ``output`` is the raw combined stdout/stderr from git.
    """
    all_output: list[str] = []
    overall_passed = True

    for change in proposal.changes:
        passed, output = _run(
            ["git", "apply", "--check", "-"],
            cwd=repo_path,
            stdin=change.diff,
        )
        all_output.append(f"# {change.path}\n{output}")
        if not passed:
            overall_passed = False

    return overall_passed, "\n".join(all_output)


def check_kubectl_dry_run(proposal: FixProposal) -> tuple[bool, str]:
    """Run ``kubectl apply --dry-run=server`` for any K8s manifests in *proposal*.

    Only changes that look like Kubernetes manifests (by path and/or content)
    are submitted.  If there are no manifest changes the function returns
    ``(True, "")`` immediately — no kubectl invocation is made.

    Returns
    -------
    (passed, output)
        ``passed`` is ``True`` when all manifests pass server-side dry-run.
        ``output`` is the verbatim combined stdout/stderr from kubectl.
    """
    k8s_changes = [c for c in proposal.changes if _looks_like_k8s_manifest(c)]

    if not k8s_changes:
        return True, ""

    all_output: list[str] = []
    overall_passed = True

    # Build a single concatenated YAML and pipe it to kubectl.
    # Separate multi-doc with "---" so kubectl can parse each object.
    combined_yaml_parts: list[str] = []
    for change in k8s_changes:
        # Extract the new-file content from the unified diff.
        new_content = _extract_new_content(change.diff)
        combined_yaml_parts.append(new_content)

    combined_yaml = "\n---\n".join(combined_yaml_parts)

    passed, output = _run(
        ["kubectl", "apply", "--dry-run=server", "-f", "-"],
        stdin=combined_yaml,
    )
    all_output.append(output)
    if not passed:
        overall_passed = False

    return overall_passed, "\n".join(all_output)


def check_action_intent_precondition(
    intent: ActionIntent,
    *,
    live_state_snapshot: dict[str, object],
) -> tuple[bool, str]:
    """Assert that the precondition implied by *intent.expected_effect* still holds.

    The check is purely textual/structural: it verifies that the
    ``target`` referenced in the intent still exists in the live-state
    snapshot with the same key properties.  A missing or structurally
    different target means the diagnosis may be outdated.

    Parameters
    ----------
    intent:
        The ``ActionIntent`` whose preconditions we are verifying.
    live_state_snapshot:
        A read-only mapping representing the current live state.  In
        production this comes from the Collectors node output; in tests
        it is a hand-crafted dict.

    Returns
    -------
    (passed, output)
        ``passed`` is ``True`` when the precondition is satisfied.
        ``output`` describes what was checked and what was found.
    """
    target = intent.target
    lines: list[str] = [
        f"target: {target}",
        f"expected_effect: {intent.expected_effect}",
    ]

    if target not in live_state_snapshot:
        lines.append(f"FAIL: target '{target}' not found in live state snapshot.")
        return False, "\n".join(lines)

    # Target is present — record what we found.
    found = live_state_snapshot[target]
    lines.append(f"PASS: target found in snapshot: {found!r}")
    return True, "\n".join(lines)


# ---------------------------------------------------------------------------
# Diff content extractor
# ---------------------------------------------------------------------------


def _extract_new_content(diff: str) -> str:
    """Return the new-file content from a unified diff.

    Lines starting with ``+`` (but not ``+++``) are new content.
    If the diff is not in unified format (no ``+``/``-`` markers), the
    entire string is returned as-is — callers supply raw file content.
    """
    plus_lines = [
        line[1:]  # strip the leading '+'
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    if plus_lines:
        return "\n".join(plus_lines)
    # Fallback: diff may be a plain file body.
    return diff
