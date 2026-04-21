"""Verifier node — runs dry-run checks and returns a typed ``VerifierResult``.

The node is LLM-driven: it presents the check results to the model and asks it
to pick the appropriate ``VerifierResultKind`` following the decision rule in
``prompts/verifier.md``:

  - All checks pass → ``passed``
  - Patch failed but diagnosis is defensible → ``implementation_error``
  - Live state contradicts root-cause → ``diagnosis_invalidated``
  - Prefer ``diagnosis_invalidated`` when uncertain.

Two subject types are handled:

``FixProposal``
    Passed in via a :func:`make_verifier_node` closure so the graph can inject
    a proposal without mutating ``IncidentState`` (schema is frozen).  Runs
    ``git apply --check`` and, for K8s manifests, ``kubectl apply --dry-run=server``.

``ActionIntent``
    Read from ``IncidentState.action_intents`` (last ``pending`` intent).  Runs a
    precondition check against a live-state snapshot injected the same way.

When neither subject is available the node returns ``passed`` with a noop
explanation — this preserves the skeleton's behaviour so the graph can still
compile and traverse the edge end-to-end during tests.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import structlog

from agent.llm.client import Client
from agent.llm.settings import LLMSettings
from agent.llm.structured import complete_typed
from agent.orchestrator.nodes._models import VerifierLLMOutput
from agent.orchestrator.verifier.checks import (
    check_action_intent_precondition,
    check_git_apply,
    check_kubectl_dry_run,
)
from agent.prompts import load as load_prompt
from agent.schemas import (
    IncidentPhase,
    IncidentState,
    TimelineEvent,
    VerifierResult,
    VerifierResultKind,
)
from agent.schemas.incident import ActionIntent
from agent.schemas.remediation import FixProposal

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_CHECKS_RUN_GIT = "git.apply.check"
_CHECKS_RUN_KUBECTL = "kubectl.apply.dry-run.server"
_CHECKS_RUN_PRECONDITION = "action_intent.precondition"


def _build_user_message(
    state: IncidentState,
    *,
    fix_proposal: FixProposal | None,
    intent: ActionIntent | None,
    check_results: list[tuple[str, bool, str]],
) -> str:
    """Compose the user-turn message handed to the LLM."""
    lines: list[str] = [
        "## Incident context",
        f"- incident_id: {state.incident_id}",
        f"- service: {state.alert.service}",
        f"- phase: {state.phase}",
        "",
        "## Findings summary",
    ]
    if state.findings:
        for f in state.findings[:5]:  # cap for context length
            lines.append(f"- [{f.collector_name}] {f.summary}")
    else:
        lines.append("- (none recorded)")

    lines += [
        "",
        "## Subject under review",
    ]
    if fix_proposal is not None:
        lines.append(f"FixProposal id={fix_proposal.id!r} plan_id={fix_proposal.plan_id!r}")
        lines.append(f"Branch: {fix_proposal.branch_name!r}")
        lines.append("Changes:")
        for c in fix_proposal.changes:
            lines.append(f"  - {c.path}")
    elif intent is not None:
        lines.append(f"ActionIntent target={intent.target!r} type={intent.action_type!r}")
        lines.append(f"Expected effect: {intent.expected_effect}")
    else:
        lines.append("(no subject — noop verification)")

    lines += [
        "",
        "## Dry-run check results",
    ]
    for check_name, passed, output in check_results:
        status = "PASS" if passed else "FAIL"
        lines.append(f"### {check_name}: {status}")
        if output:
            lines.append("```")
            lines.append(output[:4000])  # guard against huge kubectl output
            lines.append("```")

    lines += [
        "",
        "## Task",
        (
            "Based on the findings and check results above, return a VerifierLLMOutput "
            "with the correct kind.  Follow the decision rule from your system prompt: "
            "prefer `diagnosis_invalidated` over `implementation_error` when uncertain."
        ),
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Node factory — the public entry point
# ---------------------------------------------------------------------------


def make_verifier_node(
    *,
    fix_proposal: FixProposal | None = None,
    repo_path: str = ".",
    live_state_snapshot: dict[str, object] | None = None,
    llm_client: Client | None = None,
) -> Any:
    """Return a configured ``verifier_node`` function ready for the graph.

    Parameters
    ----------
    fix_proposal:
        The ``FixProposal`` produced by the Dev node.  ``None`` when the
        verifier is checking an ``ActionIntent`` instead.
    repo_path:
        Absolute path to the repository working tree for ``git apply --check``.
    live_state_snapshot:
        Mapping of target → state data used by the precondition check.
        Defaults to an empty dict (all targets fail the check).
    llm_client:
        Pre-constructed ``Client`` instance.  Created from env vars when absent.
    """
    _snapshot: dict[str, object] = live_state_snapshot or {}

    async def _verifier_node(state: IncidentState) -> dict[str, object]:
        now = datetime.now(UTC)

        # ------------------------------------------------------------------
        # 1. Determine subject and run deterministic checks.
        # ------------------------------------------------------------------
        checks_run: list[str] = []
        check_results: list[tuple[str, bool, str]] = []
        active_intent: ActionIntent | None = None

        if fix_proposal is not None:
            # FixProposal path: git apply + optional kubectl dry-run.
            git_passed, git_output = check_git_apply(fix_proposal, repo_path=repo_path)
            checks_run.append(_CHECKS_RUN_GIT)
            check_results.append((_CHECKS_RUN_GIT, git_passed, git_output))

            kube_passed, kube_output = check_kubectl_dry_run(fix_proposal)
            if kube_output:  # only count it if manifests were present
                checks_run.append(_CHECKS_RUN_KUBECTL)
                check_results.append((_CHECKS_RUN_KUBECTL, kube_passed, kube_output))

            dry_run_output = "\n".join(o for _, _, o in check_results if o)

        elif state.action_intents:
            # ActionIntent path: use the last pending intent.
            pending = [i for i in state.action_intents if i.approval_status == "pending"]
            active_intent = pending[-1] if pending else state.action_intents[-1]

            precond_passed, precond_output = check_action_intent_precondition(
                active_intent, live_state_snapshot=_snapshot
            )
            checks_run.append(_CHECKS_RUN_PRECONDITION)
            check_results.append((_CHECKS_RUN_PRECONDITION, precond_passed, precond_output))
            dry_run_output = precond_output

        else:
            # No subject — return noop pass (skeleton behaviour preserved).
            log.info("verifier.noop", incident_id=state.incident_id)
            result = VerifierResult(
                kind=VerifierResultKind.passed,
                checks_run=["noop"],
                reasoning="No FixProposal or ActionIntent present; verification skipped.",
            )
            timeline = [
                *state.timeline,
                TimelineEvent(
                    ts=now,
                    actor="orchestrator:verifier",
                    event_type="verifier.result",
                    ref_id=result.kind,
                ),
            ]
            return {
                "phase": IncidentPhase.verifying,
                "timeline": timeline,
                "updated_at": now,
                "verifier_result": result,
            }

        # ------------------------------------------------------------------
        # 2. Call the LLM to interpret the check results.
        # ------------------------------------------------------------------
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        client = llm_client or Client(
            settings=LLMSettings(),
            api_key=api_key,
        )

        bundle = load_prompt("verifier")
        system = f"{bundle.system_prefix}\n\n{bundle.role_prompt}"

        user_message = _build_user_message(
            state,
            fix_proposal=fix_proposal,
            intent=active_intent,
            check_results=check_results,
        )

        llm_output: VerifierLLMOutput = await complete_typed(
            client,
            system=system,
            messages=[{"role": "user", "content": user_message}],
            output_model=VerifierLLMOutput,
        )

        # ------------------------------------------------------------------
        # 3. Assemble VerifierResult and update timeline.
        # ------------------------------------------------------------------
        result = VerifierResult(
            kind=llm_output.kind,
            checks_run=checks_run,
            failures=llm_output.failures,
            dry_run_output=dry_run_output,
            reasoning=llm_output.reasoning,
        )

        log.info(
            "verifier.result",
            incident_id=state.incident_id,
            kind=result.kind,
            checks=checks_run,
        )

        timeline = [
            *state.timeline,
            TimelineEvent(
                ts=now,
                actor="orchestrator:verifier",
                event_type="verifier.result",
                ref_id=result.kind,
            ),
        ]

        return {
            "phase": IncidentPhase.verifying,
            "timeline": timeline,
            "updated_at": now,
            "verifier_result": result,
        }

    return _verifier_node


# ---------------------------------------------------------------------------
# Default instance — used by graph.py; tests can swap via make_verifier_node.
# ---------------------------------------------------------------------------

verifier_node = make_verifier_node()
