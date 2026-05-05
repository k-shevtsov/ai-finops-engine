# tests/conftest.py
# Shared pytest fixtures for AI FinOps Engine

from unittest.mock import MagicMock, patch

import pytest

from src.collector import ContainerMetrics


@pytest.fixture
def sample_metrics() -> ContainerMetrics:
    """Heavily over-provisioned container — CPU limit 2000m, actual usage ~15m."""
    return ContainerMetrics(
        namespace="default",
        deployment="waste-demo-1",
        container="nginx",
        pod="waste-demo-1-abc123",
        cpu_usage_cores=0.015,  # 15m actual
        memory_usage_bytes=30 * 1024**2,  # 30Mi actual
        cpu_request_cores=0.200,  # 200m request
        cpu_limit_cores=2.000,  # 2000m limit
        memory_request_bytes=256 * 1024**2,  # 256Mi request
        memory_limit_bytes=1024 * 1024**2,  # 1Gi limit
        cpu_utilization=0.015 / 0.200,  # 7.5%
        memory_utilization=30 / 256,  # ~11.7%
        cpu_limit_ratio=2.000 / 0.200,  # 10x
        memory_limit_ratio=1024 / 256,  # 4x
        cpu_throttling_rate=0.0,
        oom_events_24h=0,
        cpu_waste_cores=0.200 - 0.015,
        memory_waste_bytes=(256 - 30) * 1024**2,
    )


@pytest.fixture
def under_provisioned_metrics() -> ContainerMetrics:
    """Under-provisioned container — CPU limit too low, high throttling."""
    return ContainerMetrics(
        namespace="default",
        deployment="waste-demo-3",
        container="nginx",
        pod="waste-demo-3-xyz789",
        cpu_usage_cores=0.025,
        memory_usage_bytes=20 * 1024**2,
        cpu_request_cores=0.010,  # 10m — too low
        cpu_limit_cores=0.020,  # 20m — too low
        memory_request_bytes=16 * 1024**2,
        memory_limit_bytes=32 * 1024**2,
        cpu_utilization=2.5,  # > 1.0 — throttled
        memory_utilization=1.25,
        cpu_limit_ratio=2.0,
        memory_limit_ratio=2.0,
        cpu_throttling_rate=0.35,  # 35% throttled
        oom_events_24h=0,
        cpu_waste_cores=0.0,
        memory_waste_bytes=0.0,
    )


@pytest.fixture
def oom_metrics() -> ContainerMetrics:
    """Container with OOM kills."""
    return ContainerMetrics(
        namespace="default",
        deployment="leaky-service",
        container="app",
        pod="leaky-service-def456",
        cpu_usage_cores=0.050,
        memory_usage_bytes=900 * 1024**2,  # 900Mi — near limit
        cpu_request_cores=0.100,
        cpu_limit_cores=0.500,
        memory_request_bytes=256 * 1024**2,
        memory_limit_bytes=1024 * 1024**2,
        cpu_utilization=0.5,
        memory_utilization=3.5,  # way over request
        cpu_limit_ratio=5.0,
        memory_limit_ratio=4.0,
        cpu_throttling_rate=0.0,
        oom_events_24h=3,
        cpu_waste_cores=0.0,
        memory_waste_bytes=0.0,
    )


@pytest.fixture
def mock_prometheus_client():
    """Mock PrometheusClient that returns empty results by default."""
    with patch("src.collector.PrometheusClient") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.query.return_value = []
        mock_instance.query_value.return_value = {}
        mock_cls.return_value = mock_instance
        yield mock_instance
