"""Unit tests for the collectors dispatch node and its sub-modules.

Covers:
- Selection rules (registry)
- Cache hit / miss logic (cache)
- 500 / network-error fallback Finding (http)
- Time-range derivation (collectors_node)
- services_touched merge (collectors_node)

No real HTTP traffic (respx) and no real Postgres (mocked CollectorCache).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx

from agent.orchestrator.dispatch.cache import make_cache_key
from agent.orchestrator.dispatch.http import dispatch
from agent.orchestrator.dispatch.registry import (
    COLLECTOR_KUBE,
    COLLECTOR_LOKI,
    COLLECTOR_PROM,
    select_collector,
)
from agent.orchestrator.nodes.collectors import _build_time_range, collectors_node
from agent.schemas import (
    Alert,
    CollectorInput,
    CollectorOutput,
    EnvironmentFingerprint,
    Finding,
    Hypothesis,
    HypothesisStatus,
    IncidentPhase,
    IncidentState,
    Severity,
    TimeRange,
)
from agent.schemas.evidence import EvidenceRef

# ---------------------------------------------------------------------------
# Shared fixtures / factories
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 21, 14, 0, 0, tzinfo=UTC)


def _alert(service: str = "demo") -> Alert:
    return Alert(
        source="alertmanager",
        raw_message="OOMKilled",
        service=service,
        severity=Severity.high,
        fired_at=_NOW,
    )


def _state(
    *,
    hypothesis_text: str = "metric.memory.leak suspected",
    focus_id: str | None = "hyp_1",
    services_touched: list[str] | None = None,
    findings: list[Finding] | None = None,
) -> IncidentState:
    hyp = Hypothesis(
        id="hyp_1",
        text=hypothesis_text,
        score=0.8,
        status=HypothesisStatus.open,
        created_at=_NOW,
    )
    return IncidentState(
        incident_id="inc_001",
        alert=_alert(),
        hypotheses=[hyp],
        findings=findings or [],
        services_touched=services_touched or [],
        current_focus_hypothesis_id=focus_id,
        phase=IncidentPhase.investigating,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _env_fp() -> EnvironmentFingerprint:
    return EnvironmentFingerprint(
        cluster="k3d-lab",
        account="local",
        region="local",
        deploy_revision="abc123",
        rollout_generation="1",
    )


def _time_range() -> TimeRange:
    return TimeRange(start=_NOW - timedelta(minutes=10), end=_NOW)


def _collector_input(
    question: str = "metric.memory.leak suspected",
) -> CollectorInput:
    return CollectorInput(
        incident_id="inc_001",
        question=question,
        hypothesis_id="hyp_1",
        time_range=_time_range(),
        scope_services=["demo"],
        environment_fingerprint=_env_fp(),
    )


def _evidence_ref() -> EvidenceRef:
    return EvidenceRef(
        evidence_id="ev_001",
        storage_uri="s3://incidents/ev_001",
        content_type="application/json",
        size_bytes=42,
        expires_at=_NOW + timedelta(days=30),
    )


def _finding(
    fid: str = "find_001",
    collector: str = "prom",
    confidence: float = 0.9,
) -> Finding:
    return Finding(
        id=fid,
        collector_name=collector,
        question="metric.memory.leak suspected",
        summary="Memory usage growing at 2 MB/s",
        evidence_id="ev_001",
        confidence=confidence,
        created_at=_NOW,
    )


def _collector_output() -> CollectorOutput:
    return CollectorOutput(
        finding=_finding(),
        evidence=_evidence_ref(),
        tool_calls_used=3,
        tokens_used=512,
    )


# ===========================================================================
# Registry selection rules
# ===========================================================================


class TestSelectCollector:
    def test_metric_prefix_selects_prom(self) -> None:
        assert select_collector("metric.memory.leak") == COLLECTOR_PROM

    def test_log_prefix_selects_loki(self) -> None:
        assert select_collector("log.error.rate.spike") == COLLECTOR_LOKI

    def test_pod_prefix_selects_kube(self) -> None:
        assert select_collector("pod.crashloop.detected") == COLLECTOR_KUBE

    def test_event_prefix_selects_kube(self) -> None:
        assert select_collector("event.oomkill.happened") == COLLECTOR_KUBE

    def test_unknown_prefix_defaults_to_prom(self) -> None:
        assert select_collector("unknown.thing.happened") == COLLECTOR_PROM

    def test_empty_string_defaults_to_prom(self) -> None:
        assert select_collector("") == COLLECTOR_PROM

    def test_case_insensitive(self) -> None:
        assert select_collector("LOG.error.rate") == COLLECTOR_LOKI
        assert select_collector("METRIC.cpu") == COLLECTOR_PROM
        assert select_collector("POD.restart") == COLLECTOR_KUBE


# ===========================================================================
# Cache key stability
# ===========================================================================


class TestMakeCacheKey:
    def test_same_inputs_produce_same_key(self) -> None:
        fp = _env_fp()
        tr = _time_range()
        key1 = make_cache_key(
            incident_id="inc_001",
            collector_name="prom",
            question="metric.memory",
            time_range=tr,
            scope_services=["demo"],
            environment_fingerprint=fp,
        )
        key2 = make_cache_key(
            incident_id="inc_001",
            collector_name="prom",
            question="metric.memory",
            time_range=tr,
            scope_services=["demo"],
            environment_fingerprint=fp,
        )
        assert key1 == key2

    def test_different_question_produces_different_key(self) -> None:
        fp = _env_fp()
        tr = _time_range()
        kwargs = {
            "incident_id": "inc_001",
            "collector_name": "prom",
            "time_range": tr,
            "scope_services": ["demo"],
            "environment_fingerprint": fp,
        }
        key1 = make_cache_key(question="metric.memory", **kwargs)
        key2 = make_cache_key(question="metric.cpu", **kwargs)
        assert key1 != key2

    def test_scope_services_order_independent(self) -> None:
        fp = _env_fp()
        tr = _time_range()
        kwargs = {
            "incident_id": "inc_001",
            "collector_name": "prom",
            "question": "q",
            "time_range": tr,
            "environment_fingerprint": fp,
        }
        key1 = make_cache_key(scope_services=["svc-a", "svc-b"], **kwargs)
        key2 = make_cache_key(scope_services=["svc-b", "svc-a"], **kwargs)
        assert key1 == key2

    def test_returns_hex_string(self) -> None:
        key = make_cache_key(
            incident_id="i",
            collector_name="prom",
            question="q",
            time_range=_time_range(),
            scope_services=[],
            environment_fingerprint=_env_fp(),
        )
        assert isinstance(key, str)
        assert len(key) == 64  # SHA-256 hex digest


# ===========================================================================
# HTTP dispatch
# ===========================================================================


class TestDispatch:
    @respx.mock
    async def test_success_returns_collector_output(self) -> None:
        output = _collector_output()
        respx.post("http://localhost:8081/collect").mock(
            return_value=httpx.Response(200, text=output.model_dump_json())
        )
        result = await dispatch("prom", _collector_input())
        assert isinstance(result, CollectorOutput)
        assert result.finding.confidence == 0.9

    @respx.mock
    async def test_500_returns_failure_finding(self) -> None:
        respx.post("http://localhost:8081/collect").mock(
            return_value=httpx.Response(500, text="internal error")
        )
        result = await dispatch("prom", _collector_input())
        assert isinstance(result, Finding)
        assert result.confidence == 0.0
        assert "500" in result.summary

    @respx.mock
    async def test_loki_uses_loki_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COLLECTOR_LOKI_URL", "http://loki-svc:8082")
        output = _collector_output()
        route = respx.post("http://loki-svc:8082/collect").mock(
            return_value=httpx.Response(200, text=output.model_dump_json())
        )
        result = await dispatch("loki", _collector_input())
        assert isinstance(result, CollectorOutput)
        assert route.called

    @respx.mock
    async def test_kube_uses_kube_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COLLECTOR_KUBE_URL", "http://kube-svc:8083")
        output = _collector_output()
        route = respx.post("http://kube-svc:8083/collect").mock(
            return_value=httpx.Response(200, text=output.model_dump_json())
        )
        result = await dispatch("kube", _collector_input())
        assert isinstance(result, CollectorOutput)
        assert route.called

    @respx.mock
    async def test_timeout_returns_failure_finding(self) -> None:
        respx.post("http://localhost:8081/collect").mock(
            side_effect=httpx.TimeoutException("timed out")
        )
        result = await dispatch("prom", _collector_input())
        assert isinstance(result, Finding)
        assert result.confidence == 0.0
        assert "timeout" in result.summary.lower()

    @respx.mock
    async def test_invalid_json_returns_failure_finding(self) -> None:
        respx.post("http://localhost:8081/collect").mock(
            return_value=httpx.Response(200, text="not json at all")
        )
        result = await dispatch("prom", _collector_input())
        assert isinstance(result, Finding)
        assert result.confidence == 0.0

    @respx.mock
    async def test_injected_http_client_is_used(self) -> None:
        output = _collector_output()
        route = respx.post("http://localhost:8081/collect").mock(
            return_value=httpx.Response(200, text=output.model_dump_json())
        )
        async with httpx.AsyncClient() as client:
            result = await dispatch("prom", _collector_input(), http_client=client)
        assert isinstance(result, CollectorOutput)
        assert route.called


# ===========================================================================
# collectors_node integration (with mocked cache and respx)
# ===========================================================================


class TestCollectorsNode:
    """Tests for the full node, mocking cache and HTTP."""

    def _make_cache(
        self,
        *,
        cached_finding: Finding | None = None,
        put_raises: bool = False,
    ) -> MagicMock:
        cache = MagicMock()
        cache.get = AsyncMock(return_value=cached_finding)
        if put_raises:
            cache.put = AsyncMock(side_effect=RuntimeError("db down"))
        else:
            cache.put = AsyncMock(return_value=None)
        return cache

    @respx.mock
    async def test_successful_dispatch_merges_finding(self) -> None:
        output = _collector_output()
        respx.post("http://localhost:8081/collect").mock(
            return_value=httpx.Response(200, text=output.model_dump_json())
        )
        cache = self._make_cache()
        result = await collectors_node(_state(), cache=cache)

        findings: list[Finding] = result["findings"]  # type: ignore[assignment]
        assert len(findings) == 1
        assert findings[0].id == "find_001"
        assert findings[0].confidence == 0.9

    @respx.mock
    async def test_cache_hit_skips_http(self) -> None:
        cached = _finding(fid="find_cached")
        cache = self._make_cache(cached_finding=cached)

        # No HTTP routes registered — any real call would raise
        result = await collectors_node(_state(), cache=cache)

        findings: list[Finding] = result["findings"]  # type: ignore[assignment]
        assert len(findings) == 1
        assert findings[0].id == "find_cached"
        # cache.put must NOT be called on a hit
        cache.put.assert_not_called()

    @respx.mock
    async def test_cache_miss_calls_put_after_success(self) -> None:
        output = _collector_output()
        respx.post("http://localhost:8081/collect").mock(
            return_value=httpx.Response(200, text=output.model_dump_json())
        )
        cache = self._make_cache()
        await collectors_node(_state(), cache=cache)
        cache.put.assert_awaited_once()

    @respx.mock
    async def test_500_produces_failure_finding(self) -> None:
        respx.post("http://localhost:8081/collect").mock(
            return_value=httpx.Response(500, text="server error")
        )
        cache = self._make_cache()
        result = await collectors_node(_state(), cache=cache)

        findings: list[Finding] = result["findings"]  # type: ignore[assignment]
        assert len(findings) == 1
        assert findings[0].confidence == 0.0
        # Cache must NOT be written on failure
        cache.put.assert_not_called()

    @respx.mock
    async def test_services_touched_updated(self) -> None:
        output = _collector_output()
        respx.post("http://localhost:8081/collect").mock(
            return_value=httpx.Response(200, text=output.model_dump_json())
        )
        cache = self._make_cache()
        state = _state(services_touched=["pre-existing"])
        result = await collectors_node(state, cache=cache)

        services: list[str] = result["services_touched"]  # type: ignore[assignment]
        assert "pre-existing" in services
        assert "demo" in services  # from alert.service

    @respx.mock
    async def test_services_touched_deduped(self) -> None:
        output = _collector_output()
        respx.post("http://localhost:8081/collect").mock(
            return_value=httpx.Response(200, text=output.model_dump_json())
        )
        cache = self._make_cache()
        state = _state(services_touched=["demo", "demo"])  # already contains alert svc
        result = await collectors_node(state, cache=cache)

        services: list[str] = result["services_touched"]  # type: ignore[assignment]
        assert services.count("demo") == 1

    async def test_no_focus_hypothesis_emits_no_focus_event(self) -> None:
        state = _state(focus_id=None)
        result = await collectors_node(state, cache=self._make_cache())

        timeline = result["timeline"]  # type: ignore[assignment]
        event_types = [e.event_type for e in timeline]
        assert "collectors.dispatch.no_focus" in event_types
        assert "findings" not in result

    @respx.mock
    async def test_time_range_anchored_on_alert_fired_at(self) -> None:
        """The time range start must be alert.fired_at - 10 min."""
        state = _state()
        tr = _build_time_range(state)
        expected_start = state.alert.fired_at - timedelta(minutes=10)
        assert tr.start == expected_start

    @respx.mock
    async def test_finding_not_duplicated_on_repeat_call(self) -> None:
        """If the same finding id already exists in state, it must not be added twice."""
        existing = _finding()
        output = _collector_output()
        respx.post("http://localhost:8081/collect").mock(
            return_value=httpx.Response(200, text=output.model_dump_json())
        )
        cache = self._make_cache()
        state = _state(findings=[existing])
        result = await collectors_node(state, cache=cache)

        findings: list[Finding] = result["findings"]  # type: ignore[assignment]
        assert len(findings) == 1  # no duplicate

    @respx.mock
    async def test_loki_collector_selected_for_log_hypothesis(self) -> None:
        output = _collector_output()
        route = respx.post("http://localhost:8082/collect").mock(
            return_value=httpx.Response(200, text=output.model_dump_json())
        )
        cache = self._make_cache()
        state = _state(hypothesis_text="log.error.rate.spiking")
        await collectors_node(state, cache=cache)
        assert route.called

    @respx.mock
    async def test_kube_collector_selected_for_pod_hypothesis(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        output = _collector_output()
        monkeypatch.setenv("COLLECTOR_KUBE_URL", "http://localhost:8083")
        route = respx.post("http://localhost:8083/collect").mock(
            return_value=httpx.Response(200, text=output.model_dump_json())
        )
        cache = self._make_cache()
        state = _state(hypothesis_text="pod.crashloop.detected")
        await collectors_node(state, cache=cache)
        assert route.called

    @respx.mock
    async def test_cache_put_failure_does_not_raise(self) -> None:
        """A cache write error must be swallowed — the graph must continue."""
        output = _collector_output()
        respx.post("http://localhost:8081/collect").mock(
            return_value=httpx.Response(200, text=output.model_dump_json())
        )
        cache = self._make_cache(put_raises=True)
        # Must not raise
        result = await collectors_node(_state(), cache=cache)
        findings: list[Finding] = result["findings"]  # type: ignore[assignment]
        assert len(findings) == 1
