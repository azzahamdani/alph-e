"""Dev node — produces a ``FixProposal`` for ``type=pr`` plans.

The node receives the signed ``RemediationPlan`` from the Planner, the cited
``Finding`` entries from ``IncidentState``, and a read-only snapshot of the
target repos (local git working tree paths).  It asks the LLM to produce a
unified diff per modified file, then validates every diff syntactically via
``unidiff``.  If the LLM cannot produce valid diffs after one corrective retry
the node returns a ``BlockedReport`` — never a pseudo-fix.

LangGraph wiring note
---------------------
``IncidentState`` does not carry a ``remediation_plan`` field — the Planner
returns it from its own node dict and it is passed to the Dev node via
LangGraph ``Send``.  The public surface for tests is :func:`run_dev` which
takes all inputs explicitly; ``dev_node`` is the minimal LangGraph-compatible
wrapper that delegates to ``run_dev``.

Guardrails
----------
- Never writes to disk.
- Never modifies files outside ``plan.target_repos``.
- PR body must reference the confirmed ``Hypothesis.id``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import structlog

from agent.llm.client import Client
from agent.llm.errors import StructuredOutputError
from agent.llm.structured import complete_typed
from agent.orchestrator.dev.diff_validator import DiffValidationError, validate_file_changes
from agent.orchestrator.nodes._models import DevLLMOutput, DevOutput
from agent.prompts import loader as prompt_loader
from agent.schemas import (
    BlockedReport,
    FixProposal,
    Hypothesis,
    HypothesisStatus,
    IncidentPhase,
    IncidentState,
    RemediationPlan,
    TimelineEvent,
)
from agent.schemas.collector import Finding

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Repo snapshot helper (read-only, never writes)
# ---------------------------------------------------------------------------

_MAX_REPO_SNAPSHOT_BYTES = 32_768  # 32 KB cap to keep context manageable


def _read_repo_snapshot(repo_path: str) -> str:
    """Return a compact, read-only text snapshot of *repo_path*.

    Only reads file names and a sample of file content — never writes.
    Scoped to the provided path; returns a notice if the path does not
    exist or is not a directory.

    This is an MVP1 implementation that lists files and reads the first few
    kilobytes of each source file found.
    """
    p = Path(repo_path)
    if not p.exists() or not p.is_dir():
        return f"[repo snapshot unavailable: {repo_path!r} is not a directory]"

    lines: list[str] = [f"# Repo snapshot: {repo_path}", ""]
    total_bytes = 0

    for child in sorted(p.rglob("*")):
        if not child.is_file():
            continue
        # Skip hidden files, __pycache__, .git internals.
        parts = child.relative_to(p).parts
        if any(part.startswith(".") or part == "__pycache__" for part in parts):
            continue

        rel = child.relative_to(p)
        lines.append(f"## {rel}")
        try:
            text = child.read_text(encoding="utf-8", errors="replace")
        except OSError:
            lines.append("[unreadable]")
            continue

        remaining = _MAX_REPO_SNAPSHOT_BYTES - total_bytes
        if remaining <= 0:
            lines.append("[... snapshot size cap reached ...]")
            break

        snippet = text[:remaining]
        total_bytes += len(snippet)
        lines.append("```")
        lines.append(snippet)
        lines.append("```")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


def _build_user_message(
    plan: RemediationPlan,
    findings: list[Finding],
    confirmed_hypothesis: Hypothesis | None,
    repo_snapshots: dict[str, str],
) -> str:
    """Compose the user turn for the Dev LLM call."""
    parts: list[str] = []

    parts.append("## RemediationPlan")
    parts.append(f"- id: {plan.id}")
    parts.append(f"- type: {plan.type}")
    parts.append(f"- rationale: {plan.rationale}")
    parts.append(f"- target_repos: {plan.target_repos}")
    parts.append(f"- rollback_plan: {plan.rollback_plan or '(none)'}")
    parts.append(f"- confidence: {plan.confidence}")

    if confirmed_hypothesis:
        parts.append("")
        parts.append("## Confirmed Hypothesis")
        parts.append(f"- id: **{confirmed_hypothesis.id}**")
        parts.append(f"- text: {confirmed_hypothesis.text}")
        parts.append(f"- score: {confirmed_hypothesis.score}")

    if findings:
        parts.append("")
        parts.append("## Cited Findings")
        for f in findings:
            parts.append(f"### Finding {f.id}")
            parts.append(f"- collector: {f.collector_name}")
            parts.append(f"- question: {f.question}")
            parts.append(f"- summary: {f.summary}")
            parts.append(f"- evidence_id: {f.evidence_id}")
            parts.append(f"- confidence: {f.confidence}")

    for repo, snapshot in repo_snapshots.items():
        parts.append("")
        parts.append(f"## Repo snapshot: {repo}")
        parts.append(snapshot)

    parts.append("")
    parts.append(
        "Produce a FixProposal for this plan.  "
        "Call the devllmoutput tool with valid unified diffs for every changed file.  "
        "The PR body MUST reference the confirmed hypothesis id verbatim.  "
        "Never touch files in repos not listed in target_repos."
    )

    return "\n".join(parts)


def _corrective_diff_message(error: DiffValidationError) -> str:
    """Build the corrective user turn when diffs fail validation."""
    lines = [
        "Your previous response contained invalid unified diffs.  "
        "Fix each diff listed below and call the devllmoutput tool again.",
        "",
        "Diff validation failures:",
    ]
    for (repo, path), detail in error.failures.items():
        lines.append(f"  - {repo}/{path}: {detail}")
    lines.append("")
    lines.append(
        "A valid unified diff must start with '--- a/path' and '+++ b/path' "
        "headers followed by one or more '@@ ... @@' hunks.  "
        "Do not omit the file headers."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core async implementation
# ---------------------------------------------------------------------------


async def run_dev(
    *,
    client: Client,
    plan: RemediationPlan,
    state: IncidentState,
) -> DevOutput | BlockedReport:
    """Produce a :class:`DevOutput` (or :class:`BlockedReport`) for *plan*.

    Parameters
    ----------
    client:
        Initialised :class:`~agent.llm.client.Client`.
    plan:
        The signed ``RemediationPlan`` of ``type=pr``.
    state:
        Current ``IncidentState`` — used to extract cited findings and the
        confirmed hypothesis.

    Returns
    -------
    DevOutput
        On success: a fully-validated ``FixProposal`` with syntactically
        correct diffs and a PR body that references the confirmed hypothesis.
    BlockedReport
        When the LLM cannot produce valid diffs after one corrective retry.
    """
    log.info("dev.run.start", plan_id=plan.id, target_repos=plan.target_repos)

    # Slice: only findings cited by the plan.
    cited_ids = set(plan.evidence_ids)
    cited_findings = (
        [f for f in state.findings if f.evidence_id in cited_ids]
        if cited_ids
        else list(state.findings)
    )

    # Confirmed hypothesis (first one found; there should be exactly one).
    confirmed_hypothesis = next(
        (h for h in state.hypotheses if h.status == HypothesisStatus.confirmed),
        None,
    )

    # Read-only repo snapshots (never writes to disk).
    repo_snapshots: dict[str, str] = {}
    for repo in plan.target_repos:
        repo_snapshots[repo] = _read_repo_snapshot(repo)

    # Load system + role prompt.
    bundle = prompt_loader.load("dev")
    system_prompt = f"{bundle.system_prefix}\n\n{bundle.role_prompt}"

    user_content = _build_user_message(
        plan=plan,
        findings=cited_findings,
        confirmed_hypothesis=confirmed_hypothesis,
        repo_snapshots=repo_snapshots,
    )
    messages: list[dict[str, object]] = [{"role": "user", "content": user_content}]

    # --- Attempt 1: first LLM call ---
    what_was_tried: list[str] = []
    what_failed: list[str] = []

    llm_output: DevLLMOutput | None = None
    diff_error: DiffValidationError | None = None

    try:
        llm_output = await complete_typed(
            client,
            system=system_prompt,
            messages=messages,
            output_model=DevLLMOutput,
            max_retries=0,  # We handle the diff retry ourselves.
        )
    except StructuredOutputError as exc:
        what_was_tried.append("LLM call attempt 1 (schema validation)")
        what_failed.append(f"Schema validation failed: {exc}")
        log.warning("dev.llm.schema_error.attempt1", error=str(exc))
        return BlockedReport(
            work_item_id=f"B-04:{plan.id}",
            what_was_tried=what_was_tried,
            what_failed=what_failed,
            decision_needed=(
                "The LLM could not produce a schema-valid DevLLMOutput after the "
                "first attempt.  A human engineer should review the plan and "
                "produce the FixProposal manually."
            ),
        )

    what_was_tried.append("LLM call attempt 1")

    # Validate diffs from attempt 1.
    try:
        validate_file_changes(llm_output.changes)
    except (DiffValidationError, ValueError) as exc:
        diff_error = exc if isinstance(exc, DiffValidationError) else DiffValidationError({})
        what_failed.append(f"Diff validation attempt 1: {exc}")
        log.warning("dev.diff.invalid.attempt1", error=str(exc))
    else:
        diff_error = None

    # --- Corrective retry if diffs were invalid ---
    if diff_error is not None:
        corrective_content = _corrective_diff_message(diff_error)
        retry_messages: list[dict[str, object]] = [
            {"role": "user", "content": user_content},
            {
                "role": "assistant",
                "content": f"[Previous response had invalid diffs: {what_failed[-1]}]",
            },
            {"role": "user", "content": corrective_content},
        ]
        what_was_tried.append("LLM call attempt 2 (corrective diff retry)")

        try:
            llm_output = await complete_typed(
                client,
                system=system_prompt,
                messages=retry_messages,
                output_model=DevLLMOutput,
                max_retries=0,
            )
        except StructuredOutputError as exc:
            what_failed.append(f"Schema validation failed on retry: {exc}")
            log.warning("dev.llm.schema_error.attempt2", error=str(exc))
            return BlockedReport(
                work_item_id=f"B-04:{plan.id}",
                what_was_tried=what_was_tried,
                what_failed=what_failed,
                decision_needed=(
                    "After one corrective retry the LLM still could not produce "
                    "a schema-valid FixProposal.  Human engineer must produce the "
                    "diff manually."
                ),
            )

        try:
            validate_file_changes(llm_output.changes)
        except (DiffValidationError, ValueError) as exc:
            what_failed.append(f"Diff validation attempt 2: {exc}")
            log.warning("dev.diff.invalid.attempt2", error=str(exc))
            return BlockedReport(
                work_item_id=f"B-04:{plan.id}",
                what_was_tried=what_was_tried,
                what_failed=what_failed,
                decision_needed=(
                    "After one corrective retry the LLM still produced syntactically "
                    "invalid unified diffs.  A human engineer must write the diff."
                ),
            )

    # --- Repo-scoping guardrail ---
    allowed_repos = set(plan.target_repos)
    out_of_scope = [fc for fc in llm_output.changes if fc.repo not in allowed_repos]
    if out_of_scope:
        scoped_details = ", ".join(f"{fc.repo}/{fc.path}" for fc in out_of_scope)
        log.error("dev.out_of_scope_changes", files=scoped_details)
        return BlockedReport(
            work_item_id=f"B-04:{plan.id}",
            what_was_tried=what_was_tried,
            what_failed=[
                f"LLM attempted to modify files outside target_repos: {scoped_details}"
            ],
            decision_needed=(
                "The LLM tried to modify repos not listed in the plan.  "
                "This is a guardrail violation.  Human must review and re-scope."
            ),
        )

    # --- PR body must reference the confirmed hypothesis ---
    if confirmed_hypothesis and confirmed_hypothesis.id not in llm_output.pr_body:
        # Append a citation rather than blocking — this keeps the loop tight.
        pr_body = (
            llm_output.pr_body + f"\n\n---\n_Hypothesis ref: {confirmed_hypothesis.id}_"
        )
        log.warning(
            "dev.pr_body.missing_hypothesis_ref",
            hypothesis_id=confirmed_hypothesis.id,
        )
    else:
        pr_body = llm_output.pr_body

    # --- Build FixProposal ---
    proposal = FixProposal(
        id=f"fix_{uuid.uuid4().hex[:8]}",
        plan_id=plan.id,
        branch_name=llm_output.branch_name,
        changes=llm_output.changes,
        commit_message=llm_output.commit_message,
        pr_body=pr_body,
    )

    log.info(
        "dev.run.success",
        proposal_id=proposal.id,
        files_changed=len(proposal.changes),
    )
    return DevOutput(proposal=proposal, reasoning=llm_output.reasoning)


# ---------------------------------------------------------------------------
# LangGraph node wrapper
# ---------------------------------------------------------------------------


def dev_node(state: IncidentState) -> dict[str, object]:
    """LangGraph node entry-point for the Dev agent.

    In the current graph topology the Planner always returns ``type=none`` so
    this node is not reachable under normal flow.  It is wired in so the graph
    compiles and routing tests can traverse the Dev -> Verifier -> Reviewer path.

    When properly wired (Planner returns ``type=pr`` and passes the plan via
    ``Send``), callers should invoke :func:`run_dev` directly — it is async and
    carries the full implementation.
    """
    now = datetime.now(UTC)
    timeline = [
        *state.timeline,
        TimelineEvent(
            ts=now,
            actor="orchestrator:dev",
            event_type="dev.proposal.skipped",
            ref_id=None,
        ),
    ]
    return {
        "phase": IncidentPhase.fixing,
        "timeline": timeline,
        "updated_at": now,
    }
