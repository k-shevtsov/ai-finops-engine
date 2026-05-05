# tests/test_integration.py
# Integration tests — require real ANTHROPIC_API_KEY and k3d cluster
# Run with: make test-int

import os

import pytest

from src.agent import FinOpsAgent
from src.collector import ContainerMetrics
from src.model import AnomalyResult, AnomalyType


@pytest.mark.integration
class TestAgentIntegration:
    """Real Claude API calls — costs ~$0.002 per test."""

    def test_agent_returns_valid_recommendation_for_over_provisioned(self):
        """Full round-trip: over-provisioned metrics → Claude → recommendation."""
        metrics = ContainerMetrics(
            namespace="default",
            deployment="waste-demo-1",
            container="nginx",
            pod="waste-demo-1-abc",
            cpu_usage_cores=0.015,
            memory_usage_bytes=30 * 1024 ** 2,
            cpu_request_cores=0.200,
            cpu_limit_cores=2.000,
            memory_request_bytes=256 * 1024 ** 2,
            memory_limit_bytes=1024 * 1024 ** 2,
            cpu_utilization=0.075,
            memory_utilization=0.117,
            cpu_limit_ratio=10.0,
            memory_limit_ratio=4.0,
            cpu_throttling_rate=0.0,
            oom_events_24h=0,
        )
        anomaly = AnomalyResult(
            namespace="default",
            deployment="waste-demo-1",
            container="nginx",
            anomaly_score=-0.73,
            is_anomaly=True,
            anomaly_type=AnomalyType.OVER_PROVISIONED,
            cpu_utilization=0.075,
            memory_utilization=0.117,
            cpu_limit_ratio=10.0,
            memory_limit_ratio=4.0,
            cpu_throttling_rate=0.0,
            oom_events_24h=0,
            severity="high",
        )

        agent = FinOpsAgent(model="claude-haiku-4-5")
        rec = agent.analyze(metrics, anomaly)

        # Structural checks
        assert rec.cpu_request.endswith("m") or rec.cpu_request.replace(".", "").isdigit()
        assert rec.memory_request.endswith(("Mi", "Gi"))
        assert 0.0 <= rec.confidence <= 1.0
        assert rec.risk in ("low", "medium", "high")
        assert rec.monthly_saving_usd >= 0.0
        assert len(rec.root_cause) > 10

        # Safety floor check
        from src.agent import CPU_REQUEST_MIN_CORES, MEMORY_REQUEST_MIN_BYTES
        cpu_val = agent._parse_resource_value(rec.cpu_request, "cpu")
        mem_val = agent._parse_resource_value(rec.memory_request, "memory")
        assert cpu_val >= CPU_REQUEST_MIN_CORES
        assert mem_val >= MEMORY_REQUEST_MIN_BYTES

        print(f"\n  Recommendation: CPU {rec.cpu_request}/{rec.cpu_limit}, "
              f"MEM {rec.memory_request}/{rec.memory_limit}")
        print(f"  Saving: ${rec.monthly_saving_usd:.2f}/mo  "
              f"Confidence: {rec.confidence:.0%}  Risk: {rec.risk}")
        print(f"  Root cause: {rec.root_cause}")
