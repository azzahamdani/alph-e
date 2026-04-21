"""Unit tests for the Verifier node and its dry-run checks.

All subprocess (git/kubectl) and LLM calls are mocked — no live cluster,
no network, no ANTHROPIC_API_KEY required.

Coverage
--------
- Every routing outcome: passed / implementation_error / diagnosis_invalidated
- Ambiguity bias rule (LLM preference for diagnosis_invalidated)
- FixProposal path: git apply check + kubectl dry-run
- ActionIntent path: precondition check
- Noop path: no subject present → passed result
- check_git_apply: pass and fail cases
- check_kubectl_dry_run: no manifests, pass, fail
- check_action_intent_precondition: target present vs absent
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from agent.orchestrator.nodes.verifier import make_verifier_node
from agent.orchestrator.verifier.checks import (
    check_action_intent_precondition,
    check_git_apply,
    check_kubectl_dry_run,
)
from agent.schemas import (
    ActionIntent,
    ActionType,
    Alert,
    FileChange,
    FixProposal,
    IncidentPhase,
    IncidentState,
    Severity,
    VerifierResultKind,
)

# ---------------------------------------------------------------------------
# Fixtures / factories
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime(2026, 4, 21, 14, 0, tzinfo=UTC)


def _state(*, action_intents: list[ActionIntent] | None = None) -> IncidentState:
    return IncidentState(
        incident_id="inc_test",
        alert=Alert(
            source="alertmanager",
            raw_message="PodOOMKilled",
            service="leaky-service",
            severity=Severity.high,
            fired_at=_now(),
        ),
        action_intents=action_intents or [],
        phase=IncidentPhase.verifying,
        created_at=_now(),
        updated_at=_now(),
    )


def _fix_proposal(*, changes: list[FileChange] | None = None) -> FixProposal:
    return FixProposal(
        id="fp_001",
        plan_id="plan_001",
        branch_name="fix/oom-limit",
        commit_message="fix: raise memory limit",
        pr_body="Raises the 128Mi limit to 256Mi.",
        changes=changes
        or [
            FileChange(
                repo="my-repo",
                path="app/config.py",
                diff="--- a/app/config.py\n+++ b/app/config.py\n@@ -1 +1 @@\n-foo\n+bar",
            )
        ],
    )


def _k8s_fix_proposal() -> FixProposal:
    """FixProposal whose change touches a Kubernetes deployment manifest."""
    diff = (
        "--- a/deploy/deployment.yaml\n"
        "+++ b/deploy/deployment.yaml\n"
        "@@ -1,5 +1,5 @@\n"
        " apiVersion: apps/v1\n"
        " kind: Deployment\n"
        "-  memory: 128Mi\n"
        "+  memory: 256Mi\n"
    )
    return FixProposal(
        id="fp_002",
        plan_id="plan_001",
        branch_name="fix/oom-limit",
        commit_message="fix: raise memory limit",
        pr_body="Raises the 128Mi limit.",
        changes=[FileChange(repo="my-repo", path="deploy/deployment.yaml", diff=diff)],
    )


def _action_intent(*, target: str = "k8s:demo/leaky-service") -> ActionIntent:
    return ActionIntent(
        hash="abc123",
        action_type=ActionType.scale,
        target=target,
        parameters={"replicas": 3},
        expected_effect="leaky-service scaled to 3 replicas",
        rollback_hint="scale back to 1",
        signer="orchestrator:planner",
        signature="sig_placeholder",
        approval_status="pending",
        expires_at=datetime(2026, 4, 21, 15, 0, tzinfo=UTC),
    )


def _make_llm_response(kind: str, reasoning: str = "test", failures: list[str] | None = None) -> MagicMock:
    """Build a mock that looks like what complete_typed returns (VerifierLLMOutput)."""
    from agent.orchestrator.nodes._models import VerifierLLMOutput

    return VerifierLLMOutput(
        kind=VerifierResultKind(kind),
        reasoning=reasoning,
        failures=failures or [],
    )


# ---------------------------------------------------------------------------
# Checks unit tests — no LLM, no subprocess dependencies
# ---------------------------------------------------------------------------


class TestCheckGitApply:
    def test_pass_when_git_returns_zero(self) -> None:
        proposal = _fix_proposal()
        with patch("agent.orchestrator.verifier.checks._run") as mock_run:
            mock_run.return_value = (True, "")
            passed, output = check_git_apply(proposal, repo_path="/tmp/repo")
        assert passed is True
        assert isinstance(output, str)

    def test_fail_when_git_returns_nonzero(self) -> None:
        proposal = _fix_proposal()
        with patch("agent.orchestrator.verifier.checks._run") as mock_run:
            mock_run.return_value = (False, "error: patch failed")
            passed, output = check_git_apply(proposal, repo_path="/tmp/repo")
        assert passed is False
        assert "error: patch failed" in output

    def test_per_change_output_is_labelled(self) -> None:
        changes = [
            FileChange(repo="r", path="a.py", diff="--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y"),
            FileChange(repo="r", path="b.py", diff="--- a\n+++ b\n@@ -1 +1 @@\n-a\n+b"),
        ]
        proposal = _fix_proposal(changes=changes)
        with patch("agent.orchestrator.verifier.checks._run") as mock_run:
            mock_run.return_value = (True, "")
            _, output = check_git_apply(proposal, repo_path="/tmp/repo")
        assert "a.py" in output
        assert "b.py" in output

    def test_any_failure_makes_overall_false(self) -> None:
        changes = [
            FileChange(repo="r", path="ok.py", diff="+x"),
            FileChange(repo="r", path="bad.py", diff="+y"),
        ]
        proposal = _fix_proposal(changes=changes)
        with patch("agent.orchestrator.verifier.checks._run") as mock_run:
            mock_run.side_effect = [(True, "ok"), (False, "fail")]
            passed, _ = check_git_apply(proposal, repo_path="/tmp/repo")
        assert passed is False


class TestCheckKubectlDryRun:
    def test_no_k8s_changes_returns_pass_no_output(self) -> None:
        proposal = _fix_proposal()  # plain Python file, not a manifest
        passed, output = check_kubectl_dry_run(proposal)
        assert passed is True
        assert output == ""

    def test_k8s_manifest_runs_kubectl(self) -> None:
        proposal = _k8s_fix_proposal()
        with patch("agent.orchestrator.verifier.checks._run") as mock_run:
            mock_run.return_value = (True, "deployment.apps/leaky-service configured (dry run)")
            passed, output = check_kubectl_dry_run(proposal)
        assert passed is True
        assert "dry run" in output

    def test_kubectl_failure_returns_false(self) -> None:
        proposal = _k8s_fix_proposal()
        with patch("agent.orchestrator.verifier.checks._run") as mock_run:
            mock_run.return_value = (False, 'Error from server: "invalid memory value"')
            passed, output = check_kubectl_dry_run(proposal)
        assert passed is False
        assert "invalid memory value" in output


class TestCheckActionIntentPrecondition:
    def test_target_present_returns_pass(self) -> None:
        intent = _action_intent()
        snapshot = {"k8s:demo/leaky-service": {"replicas": 1}}
        passed, output = check_action_intent_precondition(intent, live_state_snapshot=snapshot)
        assert passed is True
        assert "PASS" in output

    def test_target_absent_returns_fail(self) -> None:
        intent = _action_intent()
        snapshot: dict[str, object] = {}
        passed, output = check_action_intent_precondition(intent, live_state_snapshot=snapshot)
        assert passed is False
        assert "FAIL" in output

    def test_output_includes_target_and_expected_effect(self) -> None:
        intent = _action_intent()
        snapshot = {"k8s:demo/leaky-service": {"replicas": 1}}
        _, output = check_action_intent_precondition(intent, live_state_snapshot=snapshot)
        assert "k8s:demo/leaky-service" in output
        assert "leaky-service scaled to 3 replicas" in output


# ---------------------------------------------------------------------------
# Verifier node routing tests — LLM is mocked
# ---------------------------------------------------------------------------


class TestVerifierNodeRouting:
    """Tests that every VerifierResultKind is reachable through the node."""

    def _run_node(
        self,
        state: IncidentState,
        *,
        fix_proposal: FixProposal | None = None,
        llm_kind: str = "passed",
        git_result: tuple[bool, str] = (True, ""),
        kubectl_result: tuple[bool, str] = (True, ""),
    ) -> dict[str, Any]:
        """Run the verifier node under fully mocked LLM and subprocess calls."""
        from agent.orchestrator.nodes._models import VerifierLLMOutput

        llm_output = VerifierLLMOutput(
            kind=VerifierResultKind(llm_kind),
            reasoning=f"Mock reasoning for {llm_kind}",
            failures=["mock failure"] if llm_kind != "passed" else [],
        )

        mock_client = MagicMock()

        node = make_verifier_node(
            fix_proposal=fix_proposal,
            repo_path="/tmp/repo",
            live_state_snapshot={"k8s:demo/leaky-service": {"replicas": 1}},
            llm_client=mock_client,
        )

        with (
            patch("agent.orchestrator.verifier.checks._run") as mock_run,
            patch(
                "agent.orchestrator.nodes.verifier.complete_typed",
                new=AsyncMock(return_value=llm_output),
            ),
            patch("agent.orchestrator.nodes.verifier.load_prompt") as mock_load,
        ):
            mock_run.return_value = git_result
            mock_bundle = MagicMock()
            mock_bundle.system_prefix = "system"
            mock_bundle.role_prompt = "role"
            mock_load.return_value = mock_bundle

            return asyncio.run(node(state))  # type: ignore[return-value]

    def test_passed_kind_is_set(self) -> None:
        state = _state()
        result = self._run_node(state, fix_proposal=_fix_proposal(), llm_kind="passed")
        vr = result["verifier_result"]
        assert vr.kind == VerifierResultKind.passed

    def test_implementation_error_kind_is_set(self) -> None:
        state = _state()
        result = self._run_node(
            state,
            fix_proposal=_fix_proposal(),
            llm_kind="implementation_error",
            git_result=(False, "patch does not apply"),
        )
        vr = result["verifier_result"]
        assert vr.kind == VerifierResultKind.implementation_error

    def test_diagnosis_invalidated_kind_is_set(self) -> None:
        state = _state()
        result = self._run_node(
            state,
            fix_proposal=_fix_proposal(),
            llm_kind="diagnosis_invalidated",
            git_result=(False, "conflicting changes"),
        )
        vr = result["verifier_result"]
        assert vr.kind == VerifierResultKind.diagnosis_invalidated

    def test_phase_set_to_verifying(self) -> None:
        state = _state()
        result = self._run_node(state, fix_proposal=_fix_proposal())
        assert result["phase"] == IncidentPhase.verifying

    def test_timeline_appended(self) -> None:
        state = _state()
        result = self._run_node(state, fix_proposal=_fix_proposal())
        events = result["timeline"]
        assert any(e.event_type == "verifier.result" for e in events)

    def test_checks_run_includes_git_check(self) -> None:
        state = _state()
        result = self._run_node(state, fix_proposal=_fix_proposal())
        vr = result["verifier_result"]
        assert "git.apply.check" in vr.checks_run

    def test_dry_run_output_is_raw_string(self) -> None:
        """dry_run_output must be the verbatim subprocess output, not a paraphrase."""
        state = _state()
        raw = "Applying patch to foo/bar.py\nHunk #1 FAILED"
        result = self._run_node(state, fix_proposal=_fix_proposal(), git_result=(False, raw))
        vr = result["verifier_result"]
        assert raw in vr.dry_run_output


class TestVerifierNodeActionIntentPath:
    def _run_node_intent(
        self,
        *,
        intent: ActionIntent,
        snapshot: dict[str, object],
        llm_kind: str = "passed",
    ) -> dict[str, Any]:
        from agent.orchestrator.nodes._models import VerifierLLMOutput

        llm_output = VerifierLLMOutput(
            kind=VerifierResultKind(llm_kind),
            reasoning="mock",
            failures=[],
        )
        state = _state(action_intents=[intent])
        mock_client = MagicMock()

        node = make_verifier_node(
            fix_proposal=None,
            live_state_snapshot=snapshot,
            llm_client=mock_client,
        )

        with patch(
            "agent.orchestrator.nodes.verifier.complete_typed",
            new=AsyncMock(return_value=llm_output),
        ), patch("agent.orchestrator.nodes.verifier.load_prompt") as mock_load:
            mock_bundle = MagicMock()
            mock_bundle.system_prefix = "sys"
            mock_bundle.role_prompt = "role"
            mock_load.return_value = mock_bundle

            return asyncio.run(node(state))  # type: ignore[return-value]

    def test_intent_precondition_pass_can_yield_passed(self) -> None:
        intent = _action_intent()
        snapshot = {"k8s:demo/leaky-service": {"replicas": 1}}
        result = self._run_node_intent(intent=intent, snapshot=snapshot, llm_kind="passed")
        vr = result["verifier_result"]
        assert vr.kind == VerifierResultKind.passed

    def test_intent_precondition_fail_can_yield_diagnosis_invalidated(self) -> None:
        """Missing target in snapshot → LLM should pick diagnosis_invalidated."""
        intent = _action_intent()
        result = self._run_node_intent(
            intent=intent,
            snapshot={},
            llm_kind="diagnosis_invalidated",
        )
        vr = result["verifier_result"]
        assert vr.kind == VerifierResultKind.diagnosis_invalidated

    def test_checks_run_includes_precondition(self) -> None:
        intent = _action_intent()
        snapshot = {"k8s:demo/leaky-service": {}}
        result = self._run_node_intent(intent=intent, snapshot=snapshot)
        vr = result["verifier_result"]
        assert "action_intent.precondition" in vr.checks_run


class TestVerifierNodeNoop:
    """No FixProposal and no ActionIntents → noop pass, no LLM call."""

    def test_noop_returns_passed(self) -> None:
        state = _state()
        node = make_verifier_node(fix_proposal=None)
        result = asyncio.run(node(state))
        vr = result["verifier_result"]
        assert vr.kind == VerifierResultKind.passed

    def test_noop_checks_run_is_noop(self) -> None:
        state = _state()
        node = make_verifier_node(fix_proposal=None)
        result = asyncio.run(node(state))
        vr = result["verifier_result"]
        assert "noop" in vr.checks_run


# ---------------------------------------------------------------------------
# Ambiguity bias rule
# ---------------------------------------------------------------------------


class TestAmbiguityBiasRule:
    """The LLM prompt specifies: when uncertain, prefer diagnosis_invalidated.

    Here we verify that the node faithfully passes through whatever the LLM
    decides — including diagnosis_invalidated when both checks fail.
    """

    def test_ambiguous_failure_routes_diagnosis_invalidated(self) -> None:
        """When both git apply AND kubectl fail, LLM may pick either error kind.

        We simulate the LLM correctly preferring diagnosis_invalidated (bias rule).
        """
        from agent.orchestrator.nodes._models import VerifierLLMOutput

        llm_output = VerifierLLMOutput(
            kind=VerifierResultKind.diagnosis_invalidated,
            reasoning=(
                "Both checks failed and I cannot determine whether the diagnosis or "
                "the patch is at fault; preferring diagnosis_invalidated per rule."
            ),
            failures=["git.apply.check failed", "kubectl.apply.dry-run.server failed"],
        )

        mock_client = MagicMock()
        state = _state()
        node = make_verifier_node(
            fix_proposal=_k8s_fix_proposal(),
            repo_path="/tmp/repo",
            llm_client=mock_client,
        )

        with (
            patch(
                "agent.orchestrator.verifier.checks._run",
                return_value=(False, "error output"),
            ),
            patch(
                "agent.orchestrator.nodes.verifier.complete_typed",
                new=AsyncMock(return_value=llm_output),
            ),
            patch("agent.orchestrator.nodes.verifier.load_prompt") as mock_load,
        ):
            mock_bundle = MagicMock()
            mock_bundle.system_prefix = "sys"
            mock_bundle.role_prompt = "role"
            mock_load.return_value = mock_bundle

            result: dict[str, Any] = asyncio.run(node(state))

        vr = result["verifier_result"]
        assert vr.kind == VerifierResultKind.diagnosis_invalidated
        assert "preferring diagnosis_invalidated" in vr.reasoning

    def test_implementation_error_only_when_llm_decides(self) -> None:
        """implementation_error is reachable — it's a valid outcome the LLM may emit."""
        from agent.orchestrator.nodes._models import VerifierLLMOutput

        llm_output = VerifierLLMOutput(
            kind=VerifierResultKind.implementation_error,
            reasoning="Patch fails but the diagnosis is clearly correct per the findings.",
            failures=["git.apply.check failed: hunk #1 rejected"],
        )

        mock_client = MagicMock()
        state = _state()
        node = make_verifier_node(
            fix_proposal=_fix_proposal(),
            repo_path="/tmp/repo",
            llm_client=mock_client,
        )

        with (
            patch(
                "agent.orchestrator.verifier.checks._run",
                return_value=(False, "hunk #1 rejected"),
            ),
            patch(
                "agent.orchestrator.nodes.verifier.complete_typed",
                new=AsyncMock(return_value=llm_output),
            ),
            patch("agent.orchestrator.nodes.verifier.load_prompt") as mock_load,
        ):
            mock_bundle = MagicMock()
            mock_bundle.system_prefix = "sys"
            mock_bundle.role_prompt = "role"
            mock_load.return_value = mock_bundle

            result: dict[str, Any] = asyncio.run(node(state))

        vr = result["verifier_result"]
        assert vr.kind == VerifierResultKind.implementation_error
