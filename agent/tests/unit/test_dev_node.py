"""Unit tests for the Dev node and diff_validator.

All LLM calls are mocked — no real network traffic.
``git apply --check`` is exercised on a known small fixture.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.llm.errors import StructuredOutputError
from agent.orchestrator.dev.diff_validator import (
    DiffValidationError,
    is_valid_diff,
    validate_file_changes,
)
from agent.orchestrator.nodes._models import DevLLMOutput, DevOutput
from agent.orchestrator.nodes.dev import dev_node, run_dev
from agent.schemas import (
    BlockedReport,
    FileChange,
    FixProposal,
    Hypothesis,
    HypothesisStatus,
    IncidentPhase,
    IncidentState,
    RemediationPlan,
    RemediationType,
    Severity,
    TimelineEvent,
)
from agent.schemas.collector import Finding
from agent.schemas.incident import Alert

# ---------------------------------------------------------------------------
# Shared diff fixtures
# ---------------------------------------------------------------------------

VALID_DIFF = textwrap.dedent("""\
    --- a/app.py
    +++ b/app.py
    @@ -1,5 +1,5 @@
     import time
    -LEAK_RATE = 2_000_000  # 2 MB/s
    +LEAK_RATE = 512_000    # 512 KB/s -- cap per plan

     def background_leak():
         while True:
""")

INVALID_DIFF_NO_HEADER = textwrap.dedent("""\
    @@ -1,3 +1,3 @@
     line one
    -old line
    +new line
""")

INVALID_DIFF_EMPTY = ""

VALID_DIFF_2 = textwrap.dedent("""\
    --- a/config.yaml
    +++ b/config.yaml
    @@ -1,3 +1,3 @@
     resources:
       limits:
    -    memory: 128Mi
    +    memory: 256Mi
""")


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _make_state(
    *,
    hypotheses: list[Hypothesis] | None = None,
    findings: list[Finding] | None = None,
) -> IncidentState:
    now = datetime.now(UTC)
    return IncidentState(
        incident_id="inc-test-001",
        alert=Alert(
            source="alertmanager",
            raw_message="PodOOMKilled: leaky-service",
            service="leaky-service",
            severity=Severity.high,
            fired_at=now,
        ),
        hypotheses=hypotheses or [],
        findings=findings or [],
        timeline=[],
        created_at=now,
        updated_at=now,
    )


def _make_plan(
    *,
    target_repos: list[str] | None = None,
    evidence_ids: list[str] | None = None,
) -> RemediationPlan:
    return RemediationPlan(
        id="plan-test-001",
        type=RemediationType.pr,
        rationale="Memory leak capped by reducing LEAK_RATE constant.",
        evidence_ids=evidence_ids or [],
        target_repos=target_repos or ["/repo/leaky-service"],
        confidence=0.85,
        requires_human_approval=False,
    )


def _make_hypothesis(*, status: HypothesisStatus = HypothesisStatus.confirmed) -> Hypothesis:
    return Hypothesis(
        id="hyp-oom-001",
        text="LEAK_RATE constant causes memory to exceed 128Mi limit",
        score=0.9,
        status=status,
        created_at=datetime.now(UTC),
    )


def _make_finding() -> Finding:
    now = datetime.now(UTC)
    return Finding(
        id="find-001",
        collector_name="prom-collector",
        question="Is memory rising at 2MB/s?",
        summary="Memory rises at 2.1 MB/s; OOM in ~60s",
        evidence_id="ev-001",
        confidence=0.95,
        created_at=now,
    )


def _make_llm_output(
    *,
    repo: str = "/repo/leaky-service",
    diff: str = VALID_DIFF,
    commit_message: str = "fix(leaky-service): cap LEAK_RATE to 512 KB/s",
    pr_body: str = "## Summary\nCap leak rate.\n## Root cause\nhyp-oom-001\n## Test plan\n- unit tests",
    branch_name: str = "fix/cap-leak-rate",
    reasoning: str = "Reduced LEAK_RATE to prevent OOM.",
) -> DevLLMOutput:
    return DevLLMOutput(
        changes=[FileChange(repo=repo, path="app.py", diff=diff)],
        commit_message=commit_message,
        pr_body=pr_body,
        branch_name=branch_name,
        reasoning=reasoning,
    )


def _make_tool_use_message(tool_name: str, payload: dict[str, Any]) -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.name = tool_name
    block.input = payload

    msg = MagicMock()
    msg.content = [block]
    return msg


def _make_client(side_effects: list[Any]) -> MagicMock:
    client = MagicMock()
    client.complete = AsyncMock(side_effect=side_effects)
    return client


def _llm_output_payload(output: DevLLMOutput) -> dict[str, Any]:
    return output.model_dump()


def _make_validation_error() -> Any:
    """Produce a real pydantic ValidationError for use in tests."""
    from pydantic import ValidationError

    try:
        DevLLMOutput.model_validate({}, strict=True)
    except ValidationError as exc:
        return exc
    raise AssertionError("Expected ValidationError was not raised")


# ---------------------------------------------------------------------------
# diff_validator unit tests
# ---------------------------------------------------------------------------


class TestDiffValidator:
    def test_valid_diff_accepted(self) -> None:
        changes = [FileChange(repo="/repo/x", path="app.py", diff=VALID_DIFF)]
        validate_file_changes(changes)  # must not raise

    def test_valid_diff_multiple_files(self) -> None:
        changes = [
            FileChange(repo="/repo/x", path="app.py", diff=VALID_DIFF),
            FileChange(repo="/repo/x", path="config.yaml", diff=VALID_DIFF_2),
        ]
        validate_file_changes(changes)  # must not raise

    def test_empty_diff_raises(self) -> None:
        changes = [FileChange(repo="/repo/x", path="app.py", diff=INVALID_DIFF_EMPTY)]
        with pytest.raises(DiffValidationError) as exc_info:
            validate_file_changes(changes)
        assert ("/repo/x", "app.py") in exc_info.value.failures

    def test_diff_missing_file_header_raises(self) -> None:
        changes = [FileChange(repo="/repo/x", path="app.py", diff=INVALID_DIFF_NO_HEADER)]
        with pytest.raises(DiffValidationError):
            validate_file_changes(changes)

    def test_empty_changes_list_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            validate_file_changes([])

    def test_multiple_failures_collected(self) -> None:
        changes = [
            FileChange(repo="/repo/x", path="a.py", diff=INVALID_DIFF_EMPTY),
            FileChange(repo="/repo/x", path="b.py", diff=INVALID_DIFF_EMPTY),
        ]
        with pytest.raises(DiffValidationError) as exc_info:
            validate_file_changes(changes)
        assert len(exc_info.value.failures) == 2

    def test_is_valid_diff_true_for_valid(self) -> None:
        assert is_valid_diff(VALID_DIFF) is True

    def test_is_valid_diff_false_for_empty(self) -> None:
        assert is_valid_diff("") is False

    def test_is_valid_diff_false_for_no_header(self) -> None:
        assert is_valid_diff(INVALID_DIFF_NO_HEADER) is False

    def test_diff_validation_error_message_includes_path(self) -> None:
        changes = [FileChange(repo="my-repo", path="src/main.py", diff="")]
        with pytest.raises(DiffValidationError) as exc_info:
            validate_file_changes(changes)
        assert "my-repo/src/main.py" in str(exc_info.value)


# ---------------------------------------------------------------------------
# run_dev happy path
# ---------------------------------------------------------------------------


class TestRunDevHappyPath:
    @pytest.mark.asyncio
    async def test_returns_dev_output_on_valid_diff(self, tmp_path: Any) -> None:
        # Write a tiny fake repo so snapshot works.
        repo = str(tmp_path)
        (tmp_path / "app.py").write_text("import time\nLEAK_RATE = 2_000_000\n")

        plan = _make_plan(target_repos=[repo])
        state = _make_state(
            hypotheses=[_make_hypothesis()],
            findings=[_make_finding()],
        )
        llm_out = _make_llm_output(repo=repo)
        payload = _llm_output_payload(llm_out)
        msg = _make_tool_use_message("devllmoutput", payload)
        client = _make_client([msg])

        result = await run_dev(client=client, plan=plan, state=state)

        assert isinstance(result, DevOutput)
        assert isinstance(result.proposal, FixProposal)
        assert result.proposal.plan_id == plan.id
        assert len(result.proposal.changes) == 1
        assert result.proposal.changes[0].repo == repo
        assert result.reasoning != ""

    @pytest.mark.asyncio
    async def test_proposal_has_conventional_commit_message(self, tmp_path: Any) -> None:
        repo = str(tmp_path)
        plan = _make_plan(target_repos=[repo])
        state = _make_state(hypotheses=[_make_hypothesis()])
        llm_out = _make_llm_output(repo=repo, commit_message="fix(svc): reduce memory cap")
        msg = _make_tool_use_message("devllmoutput", _llm_output_payload(llm_out))
        client = _make_client([msg])

        result = await run_dev(client=client, plan=plan, state=state)

        assert isinstance(result, DevOutput)
        commit_msg = result.proposal.commit_message
        # Conventional Commits: type(scope): description
        assert "(" in commit_msg and ")" in commit_msg and ": " in commit_msg

    @pytest.mark.asyncio
    async def test_pr_body_references_hypothesis_id(self, tmp_path: Any) -> None:
        repo = str(tmp_path)
        hyp = _make_hypothesis()
        plan = _make_plan(target_repos=[repo])
        state = _make_state(hypotheses=[hyp])
        pr_body_with_ref = f"## Summary\nFix.\n## Root cause\n{hyp.id}\n## Test plan\n- tests"
        llm_out = _make_llm_output(repo=repo, pr_body=pr_body_with_ref)
        msg = _make_tool_use_message("devllmoutput", _llm_output_payload(llm_out))
        client = _make_client([msg])

        result = await run_dev(client=client, plan=plan, state=state)

        assert isinstance(result, DevOutput)
        assert hyp.id in result.proposal.pr_body

    @pytest.mark.asyncio
    async def test_pr_body_gets_hypothesis_appended_when_missing(self, tmp_path: Any) -> None:
        repo = str(tmp_path)
        hyp = _make_hypothesis()
        plan = _make_plan(target_repos=[repo])
        state = _make_state(hypotheses=[hyp])
        # PR body does NOT contain the hypothesis id.
        llm_out = _make_llm_output(repo=repo, pr_body="## Summary\nNo ref here.")
        msg = _make_tool_use_message("devllmoutput", _llm_output_payload(llm_out))
        client = _make_client([msg])

        result = await run_dev(client=client, plan=plan, state=state)

        assert isinstance(result, DevOutput)
        # The node must inject the hypothesis id.
        assert hyp.id in result.proposal.pr_body


# ---------------------------------------------------------------------------
# run_dev - diff validity gate (BlockedReport on persistent failure)
# ---------------------------------------------------------------------------


class TestRunDevDiffValidityGate:
    @pytest.mark.asyncio
    async def test_invalid_diff_triggers_retry_then_blocked(self, tmp_path: Any) -> None:
        """Two consecutive calls with invalid diffs must return a BlockedReport."""
        repo = str(tmp_path)
        plan = _make_plan(target_repos=[repo])
        state = _make_state(hypotheses=[_make_hypothesis()])

        # Both attempts produce invalid diffs.
        bad_out = DevLLMOutput(
            changes=[FileChange(repo=repo, path="app.py", diff=INVALID_DIFF_EMPTY)],
            commit_message="fix(svc): nothing",
            pr_body="body",
            branch_name="fix/nothing",
            reasoning="bad",
        )
        payload = bad_out.model_dump()
        msg = _make_tool_use_message("devllmoutput", payload)
        client = _make_client([msg, msg])  # two bad responses

        result = await run_dev(client=client, plan=plan, state=state)

        assert isinstance(result, BlockedReport)
        assert "B-04" in result.work_item_id
        assert len(result.what_was_tried) >= 2
        assert any("diff" in f.lower() for f in result.what_failed)

    @pytest.mark.asyncio
    async def test_first_invalid_second_valid_returns_proposal(self, tmp_path: Any) -> None:
        """First diff is invalid; corrective retry produces a valid diff -> DevOutput."""
        repo = str(tmp_path)
        plan = _make_plan(target_repos=[repo])
        state = _make_state(hypotheses=[_make_hypothesis()])

        bad_out = DevLLMOutput(
            changes=[FileChange(repo=repo, path="app.py", diff=INVALID_DIFF_EMPTY)],
            commit_message="fix(svc): nothing",
            pr_body=f"## Root cause\n{_make_hypothesis().id}",
            branch_name="fix/nothing",
            reasoning="bad first try",
        )
        good_out = _make_llm_output(repo=repo)

        bad_msg = _make_tool_use_message("devllmoutput", bad_out.model_dump())
        good_msg = _make_tool_use_message("devllmoutput", good_out.model_dump())
        client = _make_client([bad_msg, good_msg])

        result = await run_dev(client=client, plan=plan, state=state)

        assert isinstance(result, DevOutput)
        assert client.complete.await_count == 2


# ---------------------------------------------------------------------------
# run_dev - target-repo scoping guardrail
# ---------------------------------------------------------------------------


class TestRunDevRepoScoping:
    @pytest.mark.asyncio
    async def test_out_of_scope_repo_returns_blocked(self, tmp_path: Any) -> None:
        """Changes to repos not in plan.target_repos must be rejected."""
        allowed_repo = str(tmp_path / "allowed")
        os.makedirs(allowed_repo)

        plan = _make_plan(target_repos=[allowed_repo])
        state = _make_state(hypotheses=[_make_hypothesis()])

        # LLM returns a change in a repo NOT in target_repos.
        bad_repo = "/some/other/repo"
        bad_out = _make_llm_output(repo=bad_repo)
        msg = _make_tool_use_message("devllmoutput", bad_out.model_dump())
        client = _make_client([msg])

        result = await run_dev(client=client, plan=plan, state=state)

        assert isinstance(result, BlockedReport)
        assert any("target_repos" in f for f in result.what_failed)

    @pytest.mark.asyncio
    async def test_in_scope_repo_accepted(self, tmp_path: Any) -> None:
        """Changes within target_repos are accepted."""
        repo = str(tmp_path)
        plan = _make_plan(target_repos=[repo])
        state = _make_state(hypotheses=[_make_hypothesis()])

        good_out = _make_llm_output(repo=repo)
        msg = _make_tool_use_message("devllmoutput", good_out.model_dump())
        client = _make_client([msg])

        result = await run_dev(client=client, plan=plan, state=state)

        assert isinstance(result, DevOutput)


# ---------------------------------------------------------------------------
# run_dev - commit-message format check
# ---------------------------------------------------------------------------


class TestCommitMessageFormat:
    @pytest.mark.asyncio
    async def test_commit_message_first_line_under_72_chars(self, tmp_path: Any) -> None:
        repo = str(tmp_path)
        plan = _make_plan(target_repos=[repo])
        state = _make_state(hypotheses=[_make_hypothesis()])

        # Commit message fits inside 72 chars.
        short_msg = "fix(leaky-service): cap LEAK_RATE constant"
        assert len(short_msg) <= 72

        llm_out = _make_llm_output(repo=repo, commit_message=short_msg)
        msg = _make_tool_use_message("devllmoutput", llm_out.model_dump())
        client = _make_client([msg])

        result = await run_dev(client=client, plan=plan, state=state)

        assert isinstance(result, DevOutput)
        first_line = result.proposal.commit_message.split("\n")[0]
        assert len(first_line) <= 72

    @pytest.mark.asyncio
    async def test_commit_message_has_conventional_commits_type(self, tmp_path: Any) -> None:
        repo = str(tmp_path)
        plan = _make_plan(target_repos=[repo])
        state = _make_state(hypotheses=[_make_hypothesis()])
        llm_out = _make_llm_output(repo=repo, commit_message="fix(svc): correct the bug")
        msg = _make_tool_use_message("devllmoutput", llm_out.model_dump())
        client = _make_client([msg])

        result = await run_dev(client=client, plan=plan, state=state)

        assert isinstance(result, DevOutput)
        first_line = result.proposal.commit_message.split("\n")[0]
        # Must match: type[(scope)]: description
        assert ": " in first_line, f"Missing ': ' in commit message: {first_line!r}"
        prefix = first_line.split(":")[0]
        # Conventional Commits types: fix, feat, chore, refactor, etc.
        known_types = {"fix", "feat", "chore", "refactor", "test", "docs", "style", "ci", "perf"}
        cc_type = prefix.split("(")[0]
        assert cc_type in known_types, f"Unexpected type {cc_type!r} in {first_line!r}"


# ---------------------------------------------------------------------------
# run_dev - BlockedReport on schema failure (no retry for schema errors)
# ---------------------------------------------------------------------------


class TestRunDevSchemaFailure:
    @pytest.mark.asyncio
    async def test_schema_failure_returns_blocked_immediately(self, tmp_path: Any) -> None:
        """A schema validation failure (not a diff error) immediately returns BlockedReport."""
        repo = str(tmp_path)
        plan = _make_plan(target_repos=[repo])
        state = _make_state()

        client = MagicMock()
        client.complete = AsyncMock(
            side_effect=StructuredOutputError(
                raw_output={},
                validation_error=_make_validation_error(),
            )
        )

        result = await run_dev(client=client, plan=plan, state=state)

        assert isinstance(result, BlockedReport)
        assert client.complete.await_count == 1  # No retry for schema failures.


# ---------------------------------------------------------------------------
# dev_node LangGraph wrapper
# ---------------------------------------------------------------------------


class TestDevNodeWrapper:
    def test_dev_node_returns_dict_with_phase(self) -> None:
        state = _make_state()
        result = dev_node(state)

        assert result["phase"] == IncidentPhase.fixing
        assert "timeline" in result
        assert "updated_at" in result

    def test_dev_node_appends_timeline_event(self) -> None:
        state = _make_state()
        result = dev_node(state)

        events: list[TimelineEvent] = result["timeline"]  # type: ignore[assignment]
        assert any(e.actor == "orchestrator:dev" for e in events)


# ---------------------------------------------------------------------------
# DevOutput and DevLLMOutput model tests
# ---------------------------------------------------------------------------


class TestDevModels:
    def test_dev_output_requires_proposal_and_reasoning(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            DevOutput.model_validate({}, strict=True)

    def test_dev_llm_output_requires_all_fields(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            DevLLMOutput.model_validate({}, strict=True)

    def test_dev_output_roundtrip(self) -> None:
        proposal = FixProposal(
            id="fix-abc123",
            plan_id="plan-001",
            branch_name="fix/test",
            changes=[FileChange(repo="/repo", path="a.py", diff=VALID_DIFF)],
            commit_message="fix(svc): test",
            pr_body="## Summary\nTest.\n## Root cause\nhyp-001\n## Test plan\n- run tests",
        )
        out = DevOutput(proposal=proposal, reasoning="All good.")
        dumped = out.model_dump()
        restored = DevOutput.model_validate(dumped)
        assert restored.proposal.id == proposal.id
        assert restored.reasoning == "All good."


# ---------------------------------------------------------------------------
# git apply --check integration fixture (only if git is available)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git not available",
)
class TestGitApplyCheck:
    def test_valid_diff_applies_cleanly(self, tmp_path: Any) -> None:
        """Known small repo + known root-cause diff must apply cleanly."""
        # Set up a git repo with the original file.
        subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            check=True,
            capture_output=True,
            cwd=str(tmp_path),
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            check=True,
            capture_output=True,
            cwd=str(tmp_path),
        )

        app_py = tmp_path / "app.py"
        app_py.write_text(
            "import time\n"
            "LEAK_RATE = 2_000_000  # 2 MB/s\n"
            "\n"
            "def background_leak():\n"
            "    while True:\n"
        )
        subprocess.run(
            ["git", "add", "app.py"], check=True, capture_output=True, cwd=str(tmp_path)
        )
        subprocess.run(
            ["git", "commit", "-m", "init"],
            check=True,
            capture_output=True,
            cwd=str(tmp_path),
        )

        # Write the diff to a temp file and run git apply --check.
        with tempfile.NamedTemporaryFile(
            suffix=".patch", mode="w", delete=False, dir=str(tmp_path)
        ) as pf:
            pf.write(VALID_DIFF)
            patch_path = pf.name

        result = subprocess.run(
            ["git", "apply", "--check", patch_path],
            cwd=str(tmp_path),
            capture_output=True,
        )
        assert result.returncode == 0, (
            f"git apply --check failed:\nstdout: {result.stdout.decode()}\n"
            f"stderr: {result.stderr.decode()}"
        )
