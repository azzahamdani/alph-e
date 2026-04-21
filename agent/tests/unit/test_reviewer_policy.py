"""Unit tests for the Reviewer node — policy checks and node behaviour.

All LLM calls are mocked.  No network traffic.

Coverage:
- Each of the three hard-rule rejection paths (diff_repos, commit_message,
  pr_body_evidence).
- Ambiguity default: LLM returns approve/challenge but policy overrides when
  warranted.
- challenge_root_cause with empty citations falls back to request_changes_on_fix.
- Happy path: all checks pass + LLM says approve → approve emitted.
- No proposal / no plan → skipped with passthrough.
- No confirmed hypothesis → request_changes_on_fix.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.orchestrator.nodes._models import ReviewerOutput
from agent.orchestrator.reviewer.policy import (
    PolicyViolation,
    check_commit_message,
    check_diff_repos,
    check_pr_body_evidence,
    run_all_checks,
)
from agent.schemas import (
    Alert,
    FileChange,
    Finding,
    FixProposal,
    Hypothesis,
    HypothesisStatus,
    IncidentPhase,
    IncidentState,
    RemediationPlan,
    RemediationType,
    Severity,
)

# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 21, 12, 0, tzinfo=UTC)


def _alert() -> Alert:
    return Alert(
        source="alertmanager",
        raw_message="OOMKilled",
        service="leaky-service",
        severity=Severity.high,
        fired_at=_NOW,
    )


def _state(
    *,
    hypotheses: list[Hypothesis] | None = None,
    findings: list[Finding] | None = None,
    fix_proposal: FixProposal | None = None,
    remediation_plan: RemediationPlan | None = None,
) -> IncidentState:
    base = IncidentState(
        incident_id="inc_test",
        alert=_alert(),
        hypotheses=hypotheses or [],
        findings=findings or [],
        phase=IncidentPhase.verifying,
        created_at=_NOW,
        updated_at=_NOW,
    )
    # Attach runtime fields as plain attributes (mirrors LangGraph dict-merge).
    if fix_proposal is not None:
        object.__setattr__(base, "fix_proposal", fix_proposal)
    if remediation_plan is not None:
        object.__setattr__(base, "remediation_plan", remediation_plan)
    return base


def _hypothesis(status: HypothesisStatus = HypothesisStatus.confirmed) -> Hypothesis:
    return Hypothesis(
        id="hyp-001",
        text="Memory leak causes OOM.",
        score=0.9,
        status=status,
        created_at=_NOW,
    )


def _finding(evidence_id: str = "ev-abc") -> Finding:
    return Finding(
        id="find-001",
        collector_name="prom",
        question="Is memory rising?",
        summary="Memory rises 2MB/s.",
        evidence_id=evidence_id,
        confidence=0.95,
        created_at=_NOW,
    )


def _plan(target_repos: list[str] | None = None) -> RemediationPlan:
    return RemediationPlan(
        id="plan-001",
        type=RemediationType.pr,
        rationale="Fix the leak.",
        target_repos=target_repos if target_repos is not None else ["github.com/org/repo"],
    )


def _proposal(
    *,
    repos: list[str] | None = None,
    commit_message: str = "fix: reduce allocations (hyp-001)",
    pr_body: str = "Fixes memory leak. Evidence: ev-abc.",
) -> FixProposal:
    repos = repos if repos is not None else ["github.com/org/repo"]
    changes = [
        FileChange(repo=r, path="main.go", diff="- leak\n+ no leak")
        for r in repos
    ]
    return FixProposal(
        id="prop-001",
        plan_id="plan-001",
        branch_name="fix/oom",
        changes=changes,
        commit_message=commit_message,
        pr_body=pr_body,
    )


# ---------------------------------------------------------------------------
# Hard-rule unit tests (policy.py)
# ---------------------------------------------------------------------------


class TestCheckDiffRepos:
    def test_passes_when_all_repos_in_scope(self) -> None:
        plan = _plan(target_repos=["github.com/org/repo"])
        proposal = _proposal(repos=["github.com/org/repo"])
        assert check_diff_repos(proposal, plan) is None

    def test_fails_when_repo_out_of_scope(self) -> None:
        plan = _plan(target_repos=["github.com/org/allowed"])
        proposal = _proposal(repos=["github.com/org/not-allowed"])
        result = check_diff_repos(proposal, plan)
        assert isinstance(result, PolicyViolation)
        assert result.rule == "diff_repos"
        assert "not-allowed" in result.detail

    def test_fails_when_target_repos_empty(self) -> None:
        plan = _plan(target_repos=[])
        proposal = _proposal()
        result = check_diff_repos(proposal, plan)
        assert isinstance(result, PolicyViolation)
        assert result.rule == "diff_repos"

    def test_fails_when_mixed_repos_partially_out_of_scope(self) -> None:
        plan = _plan(target_repos=["github.com/org/a"])
        proposal = _proposal(repos=["github.com/org/a", "github.com/org/b"])
        result = check_diff_repos(proposal, plan)
        assert isinstance(result, PolicyViolation)
        assert "github.com/org/b" in result.detail


class TestCheckCommitMessage:
    def test_passes_when_hypothesis_id_present(self) -> None:
        hyp = _hypothesis()
        proposal = _proposal(commit_message="fix: reduce allocations (hyp-001)")
        assert check_commit_message(proposal, hyp) is None

    def test_fails_when_hypothesis_id_absent(self) -> None:
        hyp = _hypothesis()
        proposal = _proposal(commit_message="fix: reduce allocations (hyp-999)")
        result = check_commit_message(proposal, hyp)
        assert isinstance(result, PolicyViolation)
        assert result.rule == "commit_message"
        assert "hyp-001" in result.detail

    def test_fails_when_commit_message_empty(self) -> None:
        hyp = _hypothesis()
        proposal = _proposal(commit_message="")
        result = check_commit_message(proposal, hyp)
        assert isinstance(result, PolicyViolation)


class TestCheckPrBodyEvidence:
    def test_passes_when_evidence_id_in_body(self) -> None:
        findings = [_finding(evidence_id="ev-abc")]
        proposal = _proposal(pr_body="This fix addresses ev-abc.")
        assert check_pr_body_evidence(proposal, findings) is None

    def test_fails_when_no_evidence_id_in_body(self) -> None:
        findings = [_finding(evidence_id="ev-abc")]
        proposal = _proposal(pr_body="This fix does not cite any evidence.")
        result = check_pr_body_evidence(proposal, findings)
        assert isinstance(result, PolicyViolation)
        assert result.rule == "pr_body_evidence"

    def test_fails_when_findings_empty(self) -> None:
        proposal = _proposal()
        result = check_pr_body_evidence(proposal, [])
        assert isinstance(result, PolicyViolation)
        assert result.rule == "pr_body_evidence"

    def test_passes_with_any_matching_evidence_id(self) -> None:
        findings = [
            _finding(evidence_id="ev-111"),
            _finding(evidence_id="ev-222"),
        ]
        proposal = _proposal(pr_body="See ev-222 for context.")
        assert check_pr_body_evidence(proposal, findings) is None


class TestRunAllChecks:
    def test_no_violations_on_clean_proposal(self) -> None:
        plan = _plan()
        proposal = _proposal()
        hyp = _hypothesis()
        findings = [_finding()]
        assert run_all_checks(proposal, plan, hyp, findings) == []

    def test_returns_multiple_violations_when_multiple_rules_fail(self) -> None:
        plan = _plan(target_repos=["github.com/org/allowed"])
        proposal = _proposal(
            repos=["github.com/org/bad"],
            commit_message="missing-id",
            pr_body="no evidence here",
        )
        hyp = _hypothesis()
        findings = [_finding()]
        violations = run_all_checks(proposal, plan, hyp, findings)
        # All three rules violated
        rules = {v.rule for v in violations}
        assert "diff_repos" in rules
        assert "commit_message" in rules
        assert "pr_body_evidence" in rules


# ---------------------------------------------------------------------------
# Node integration tests (reviewer_node)
# ---------------------------------------------------------------------------


def _make_tool_use_message(payload: dict[str, Any]) -> MagicMock:
    """Build a fake Anthropic Message that returns ReviewerOutput payload."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = "revieweroutput"
    block.input = payload

    msg = MagicMock()
    msg.content = [block]
    return msg


def _mock_client(payload: dict[str, Any]) -> MagicMock:
    msg = _make_tool_use_message(payload)
    client = MagicMock()
    client.complete = AsyncMock(return_value=msg)
    return client


@pytest.mark.asyncio
async def test_reviewer_node_skips_when_no_proposal() -> None:
    from agent.orchestrator.nodes.reviewer import reviewer_node

    state = _state()
    result = await reviewer_node(state)
    assert result["phase"] == IncidentPhase.reviewing
    assert any("skipped" in e.event_type for e in result["timeline"])


@pytest.mark.asyncio
async def test_reviewer_node_request_changes_when_no_confirmed_hypothesis() -> None:
    from agent.orchestrator.nodes.reviewer import reviewer_node

    state = _state(
        hypotheses=[_hypothesis(HypothesisStatus.open)],
        fix_proposal=_proposal(),
        remediation_plan=_plan(),
    )
    result = await reviewer_node(state)
    output: ReviewerOutput = result["reviewer_output"]
    assert output.decision == "request_changes_on_fix"
    assert "no confirmed hypothesis" in output.reasoning


@pytest.mark.asyncio
async def test_reviewer_node_hard_reject_diff_repos() -> None:
    from agent.orchestrator.nodes.reviewer import reviewer_node

    state = _state(
        hypotheses=[_hypothesis()],
        findings=[_finding()],
        fix_proposal=_proposal(repos=["github.com/org/bad"]),
        remediation_plan=_plan(target_repos=["github.com/org/good"]),
    )
    result = await reviewer_node(state)
    output: ReviewerOutput = result["reviewer_output"]
    assert output.decision == "request_changes_on_fix"
    assert "diff_repos" in output.reasoning


@pytest.mark.asyncio
async def test_reviewer_node_hard_reject_commit_message() -> None:
    from agent.orchestrator.nodes.reviewer import reviewer_node

    state = _state(
        hypotheses=[_hypothesis()],
        findings=[_finding()],
        fix_proposal=_proposal(commit_message="fix: no hypothesis id here"),
        remediation_plan=_plan(),
    )
    result = await reviewer_node(state)
    output: ReviewerOutput = result["reviewer_output"]
    assert output.decision == "request_changes_on_fix"
    assert "commit_message" in output.reasoning


@pytest.mark.asyncio
async def test_reviewer_node_hard_reject_pr_body_evidence() -> None:
    from agent.orchestrator.nodes.reviewer import reviewer_node

    state = _state(
        hypotheses=[_hypothesis()],
        findings=[_finding(evidence_id="ev-abc")],
        fix_proposal=_proposal(pr_body="No evidence cited here at all."),
        remediation_plan=_plan(),
    )
    result = await reviewer_node(state)
    output: ReviewerOutput = result["reviewer_output"]
    assert output.decision == "request_changes_on_fix"
    assert "pr_body_evidence" in output.reasoning


@pytest.mark.asyncio
async def test_reviewer_node_approve_on_clean_proposal() -> None:
    from agent.orchestrator.nodes.reviewer import reviewer_node

    llm_payload = {
        "decision": "approve",
        "reasoning": "Implementation matches confirmed hypothesis and evidence.",
        "cited_evidence_ids": ["ev-abc"],
    }

    state = _state(
        hypotheses=[_hypothesis()],
        findings=[_finding()],
        fix_proposal=_proposal(),
        remediation_plan=_plan(),
    )

    mock_client = _mock_client(llm_payload)
    with patch("agent.orchestrator.nodes.reviewer.Client", return_value=mock_client):
        result = await reviewer_node(state)

    output: ReviewerOutput = result["reviewer_output"]
    assert output.decision == "approve"
    assert output.cited_evidence_ids == ["ev-abc"]


@pytest.mark.asyncio
async def test_reviewer_node_challenge_with_citations_preserved() -> None:
    from agent.orchestrator.nodes.reviewer import reviewer_node

    llm_payload = {
        "decision": "challenge_root_cause",
        "reasoning": "Evidence ev-abc shows CPU not memory is the culprit.",
        "cited_evidence_ids": ["ev-abc"],
    }

    state = _state(
        hypotheses=[_hypothesis()],
        findings=[_finding()],
        fix_proposal=_proposal(),
        remediation_plan=_plan(),
    )

    mock_client = _mock_client(llm_payload)
    with patch("agent.orchestrator.nodes.reviewer.Client", return_value=mock_client):
        result = await reviewer_node(state)

    output: ReviewerOutput = result["reviewer_output"]
    assert output.decision == "challenge_root_cause"
    assert "ev-abc" in output.cited_evidence_ids


@pytest.mark.asyncio
async def test_reviewer_node_challenge_empty_citations_fallback() -> None:
    from agent.orchestrator.nodes.reviewer import reviewer_node

    llm_payload = {
        "decision": "challenge_root_cause",
        "reasoning": "Something seems off.",
        "cited_evidence_ids": [],  # empty — must fall back
    }

    state = _state(
        hypotheses=[_hypothesis()],
        findings=[_finding()],
        fix_proposal=_proposal(),
        remediation_plan=_plan(),
    )

    mock_client = _mock_client(llm_payload)
    with patch("agent.orchestrator.nodes.reviewer.Client", return_value=mock_client):
        result = await reviewer_node(state)

    output: ReviewerOutput = result["reviewer_output"]
    assert output.decision == "request_changes_on_fix"
    assert "challenge_root_cause" in output.reasoning
    assert "Falling back" in output.reasoning


@pytest.mark.asyncio
async def test_reviewer_node_timeline_records_decision() -> None:
    from agent.orchestrator.nodes.reviewer import reviewer_node

    llm_payload = {
        "decision": "approve",
        "reasoning": "All good.",
        "cited_evidence_ids": ["ev-abc"],
    }

    state = _state(
        hypotheses=[_hypothesis()],
        findings=[_finding()],
        fix_proposal=_proposal(),
        remediation_plan=_plan(),
    )

    mock_client = _mock_client(llm_payload)
    with patch("agent.orchestrator.nodes.reviewer.Client", return_value=mock_client):
        result = await reviewer_node(state)

    events = result["timeline"]
    assert any("reviewer.approve" in e.event_type for e in events)
