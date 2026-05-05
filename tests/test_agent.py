# tests/test_agent.py
# Unit tests for src/agent.py
# Claude API is fully mocked — zero API calls, zero cost

import json
from unittest.mock import MagicMock, patch

import pytest

from src.agent import FinOpsAgent, FinOpsRecommendation
from src.model import AnomalyResult, AnomalyType


def make_anomaly_result(
    anomaly_type: AnomalyType = AnomalyType.OVER_PROVISIONED,
    score: float = -0.73,
) -> AnomalyResult:
    return AnomalyResult(
        namespace="default",
        deployment="waste-demo-1",
        container="nginx",
        anomaly_score=score,
        is_anomaly=True,
        anomaly_type=anomaly_type,
        cpu_utilization=0.075,
        memory_utilization=0.117,
        cpu_limit_ratio=10.0,
        memory_limit_ratio=4.0,
        cpu_throttling_rate=0.0,
        oom_events_24h=0,
        severity="high",
    )


def make_mock_response(payload: dict) -> MagicMock:
    """Build a mock Anthropic Messages response with JSON text content."""
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = json.dumps(payload)

    response = MagicMock()
    response.stop_reason = "end_turn"
    response.content = [text_block]
    return response


VALID_RECOMMENDATION_PAYLOAD = {
    "anomaly_type": "over_provisioned",
    "severity": "high",
    "root_cause": "CPU limit is 46x higher than p95 actual usage",
    "recommendation": {
        "cpu_request": "20m",
        "cpu_limit": "100m",
        "memory_request": "40Mi",
        "memory_limit": "128Mi",
    },
    "monthly_saving_usd": 11.20,
    "confidence": 0.91,
    "risk": "low",
    "reasoning": "CPU p95 is ~20m, current limit 2000m is excessive. Safe to reduce.",
}


@pytest.fixture
def agent():
    """FinOpsAgent with mocked Anthropic client."""
    with patch("src.agent.anthropic.Anthropic"):
        a = FinOpsAgent(model="claude-haiku-4-5", api_key="test-key")
        return a


@pytest.fixture
def mock_client(agent):
    """Return the mocked Anthropic client attached to agent."""
    return agent._client


class TestFinOpsAgent:

    def test_analyze_returns_finops_recommendation(self, agent, mock_client, sample_metrics):
        mock_client.messages.create.return_value = make_mock_response(
            VALID_RECOMMENDATION_PAYLOAD
        )
        anomaly = make_anomaly_result()
        result = agent.analyze(sample_metrics, anomaly)
        assert isinstance(result, FinOpsRecommendation)

    def test_analyze_returns_correct_saving(self, agent, mock_client, sample_metrics):
        mock_client.messages.create.return_value = make_mock_response(
            VALID_RECOMMENDATION_PAYLOAD
        )
        result = agent.analyze(sample_metrics, make_anomaly_result())
        assert result.monthly_saving_usd == pytest.approx(11.20)

    def test_analyze_returns_correct_confidence(self, agent, mock_client, sample_metrics):
        mock_client.messages.create.return_value = make_mock_response(
            VALID_RECOMMENDATION_PAYLOAD
        )
        result = agent.analyze(sample_metrics, make_anomaly_result())
        assert result.confidence == pytest.approx(0.91)

    def test_analyze_respects_safety_floor_cpu(self, agent, mock_client, sample_metrics):
        payload = {**VALID_RECOMMENDATION_PAYLOAD}
        payload["recommendation"] = {**payload["recommendation"], "cpu_request": "5m"}
        mock_client.messages.create.return_value = make_mock_response(payload)

        result = agent.analyze(sample_metrics, make_anomaly_result())
        # 5m is below floor of 10m — must be clamped
        assert result.cpu_request == "10m"

    def test_analyze_respects_safety_floor_memory(self, agent, mock_client, sample_metrics):
        payload = {**VALID_RECOMMENDATION_PAYLOAD}
        payload["recommendation"] = {**payload["recommendation"], "memory_request": "8Mi"}
        mock_client.messages.create.return_value = make_mock_response(payload)

        result = agent.analyze(sample_metrics, make_anomaly_result())
        assert result.memory_request == "16Mi"

    def test_analyze_escalates_risk_for_large_cpu_limit_reduction(
        self, agent, mock_client, sample_metrics
    ):
        # sample_metrics has cpu_limit=2000m, recommendation reduces to 100m (95% reduction)
        payload = {**VALID_RECOMMENDATION_PAYLOAD, "risk": "low"}
        payload["recommendation"] = {**payload["recommendation"], "cpu_limit": "100m"}
        mock_client.messages.create.return_value = make_mock_response(payload)

        result = agent.analyze(sample_metrics, make_anomaly_result())
        # 2000m → 100m = 95% reduction → risk must be escalated from low to medium
        assert result.risk in ("medium", "high")

    def test_analyze_sets_high_risk_for_oom_events(
        self, agent, mock_client, oom_metrics
    ):
        payload = {**VALID_RECOMMENDATION_PAYLOAD, "risk": "low"}
        mock_client.messages.create.return_value = make_mock_response(payload)

        anomaly = make_anomaly_result(anomaly_type=AnomalyType.MEMORY_LEAK)
        result = agent.analyze(oom_metrics, anomaly)
        assert result.risk == "high"

    def test_analyze_handles_tool_use_response(self, agent, mock_client, sample_metrics):
        # First call returns tool_use, second returns final text
        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.name = "get_resource_history"
        tool_block.id = "tool_abc123"
        tool_block.input = {"deployment": "waste-demo-1", "namespace": "default"}

        tool_response = MagicMock()
        tool_response.stop_reason = "tool_use"
        tool_response.content = [tool_block]

        final_response = make_mock_response(VALID_RECOMMENDATION_PAYLOAD)

        mock_client.messages.create.side_effect = [tool_response, final_response]

        result = agent.analyze(sample_metrics, make_anomaly_result())
        assert isinstance(result, FinOpsRecommendation)
        assert mock_client.messages.create.call_count == 2

    def test_analyze_calls_get_resource_history_tool(
        self, agent, mock_client, sample_metrics
    ):
        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.name = "get_resource_history"
        tool_block.id = "t1"
        tool_block.input = {"deployment": "waste-demo-1", "namespace": "default"}

        tool_response = MagicMock()
        tool_response.stop_reason = "tool_use"
        tool_response.content = [tool_block]

        final_response = make_mock_response(VALID_RECOMMENDATION_PAYLOAD)
        mock_client.messages.create.side_effect = [tool_response, final_response]

        agent.analyze(sample_metrics, make_anomaly_result())
        # Verify two API calls were made (tool call + final)
        assert mock_client.messages.create.call_count == 2

    def test_fallback_recommendation_on_invalid_json(
        self, agent, mock_client, sample_metrics
    ):
        bad_response = MagicMock()
        bad_response.stop_reason = "end_turn"
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "This is not JSON at all"
        bad_response.content = [text_block]
        mock_client.messages.create.return_value = bad_response

        result = agent.analyze(sample_metrics, make_anomaly_result())
        # Fallback should return high risk and low confidence
        assert result.risk == "high"
        assert result.confidence == pytest.approx(0.3)

    def test_parse_resource_value_cpu_millicores(self, agent):
        assert agent._parse_resource_value("100m", "cpu") == pytest.approx(0.1)
        assert agent._parse_resource_value("500m", "cpu") == pytest.approx(0.5)
        assert agent._parse_resource_value("2000m", "cpu") == pytest.approx(2.0)

    def test_parse_resource_value_cpu_cores(self, agent):
        assert agent._parse_resource_value("0.5", "cpu") == pytest.approx(0.5)
        assert agent._parse_resource_value("1", "cpu") == pytest.approx(1.0)

    def test_parse_resource_value_memory_mi(self, agent):
        result = agent._parse_resource_value("64Mi", "memory")
        assert result == pytest.approx(64 * 1024 ** 2)

    def test_parse_resource_value_memory_gi(self, agent):
        result = agent._parse_resource_value("1Gi", "memory")
        assert result == pytest.approx(1024 ** 3)

    def test_tool_get_resource_history_returns_percentiles(
        self, agent, sample_metrics
    ):
        result = agent._tool_get_resource_history(sample_metrics)
        assert "cpu_p95_m" in result
        assert "memory_p95_mi" in result
        assert result["cpu_p95_m"] > 0
