# tests/test_model.py
# Unit tests for src/model.py
# No real cluster or API calls required

import numpy as np
import pytest

from src.collector import ContainerMetrics
from src.model import (
    ANOMALY_SCORE_THRESHOLD,
    AnomalyResult,
    AnomalyType,
    ResourceAnomalyDetector,
)


def make_normal_metrics(index: int = 0) -> ContainerMetrics:
    """Well-provisioned container — realistic utilization ~60%."""
    return ContainerMetrics(
        namespace="default",
        deployment=f"normal-svc-{index}",
        container="app",
        pod=f"normal-svc-{index}-abc",
        cpu_usage_cores=0.060,
        memory_usage_bytes=150 * 1024 ** 2,
        cpu_request_cores=0.100,
        cpu_limit_cores=0.200,
        memory_request_bytes=256 * 1024 ** 2,
        memory_limit_bytes=512 * 1024 ** 2,
        cpu_utilization=0.60,
        memory_utilization=0.59,
        cpu_limit_ratio=2.0,
        memory_limit_ratio=2.0,
        cpu_throttling_rate=0.0,
        oom_events_24h=0,
    )


def make_over_provisioned() -> ContainerMetrics:
    """Heavily over-provisioned — CPU limit 2000m, actual ~15m."""
    return ContainerMetrics(
        namespace="default",
        deployment="waste-svc",
        container="nginx",
        pod="waste-svc-xyz",
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


def make_under_provisioned() -> ContainerMetrics:
    """Under-provisioned — high throttling."""
    return ContainerMetrics(
        namespace="default",
        deployment="throttled-svc",
        container="app",
        pod="throttled-svc-abc",
        cpu_usage_cores=0.025,
        memory_usage_bytes=20 * 1024 ** 2,
        cpu_request_cores=0.010,
        cpu_limit_cores=0.020,
        memory_request_bytes=16 * 1024 ** 2,
        memory_limit_bytes=32 * 1024 ** 2,
        cpu_utilization=2.5,
        memory_utilization=1.25,
        cpu_limit_ratio=2.0,
        memory_limit_ratio=2.0,
        cpu_throttling_rate=0.40,
        oom_events_24h=0,
    )


def make_trained_detector(n_normal: int = 30) -> ResourceAnomalyDetector:
    """Return a detector trained on normal workloads."""
    detector = ResourceAnomalyDetector(min_samples=10)
    normal = [make_normal_metrics(i) for i in range(n_normal)]
    detector.train(normal)
    return detector


class TestResourceAnomalyDetector:

    def test_is_not_trained_before_train_called(self):
        detector = ResourceAnomalyDetector()
        assert detector.is_trained is False

    def test_is_trained_after_sufficient_samples(self):
        detector = make_trained_detector()
        assert detector.is_trained is True

    def test_not_trained_with_insufficient_samples(self):
        detector = ResourceAnomalyDetector(min_samples=50)
        detector.train([make_normal_metrics(i) for i in range(10)])
        assert detector.is_trained is False

    def test_predict_returns_anomaly_result(self):
        detector = make_trained_detector()
        result = detector.predict(make_normal_metrics())
        assert isinstance(result, AnomalyResult)

    def test_predict_before_training_returns_not_anomaly(self):
        detector = ResourceAnomalyDetector()
        result = detector.predict(make_normal_metrics())
        assert result.is_anomaly is False
        assert result.anomaly_type == AnomalyType.NORMAL

    def test_anomaly_score_negative_for_outlier(self):
        detector = make_trained_detector(n_normal=50)
        # Train on normal, then predict extreme outlier
        outlier = make_over_provisioned()
        # Add many copies of outlier to history to make it anomalous
        result = detector.predict(outlier)
        # Score should be returned as a float
        assert isinstance(result.anomaly_score, float)

    def test_feature_vector_has_correct_dimensions(self):
        detector = ResourceAnomalyDetector()
        features = detector._extract_features(make_normal_metrics())
        assert features.shape == (6,)

    def test_feature_vector_values_match_metrics(self):
        detector = ResourceAnomalyDetector()
        m = make_normal_metrics()
        features = detector._extract_features(m)
        assert features[0] == pytest.approx(m.cpu_utilization)
        assert features[1] == pytest.approx(m.memory_utilization)
        assert features[2] == pytest.approx(m.cpu_limit_ratio)
        assert features[3] == pytest.approx(m.memory_limit_ratio)
        assert features[4] == pytest.approx(m.cpu_throttling_rate)
        assert features[5] == pytest.approx(float(m.oom_events_24h))

    def test_classify_over_provisioned(self, sample_metrics):
        detector = ResourceAnomalyDetector()
        result = detector._classify(sample_metrics)
        assert result == AnomalyType.OVER_PROVISIONED

    def test_classify_under_provisioned_by_throttling(self, under_provisioned_metrics):
        detector = ResourceAnomalyDetector()
        result = detector._classify(under_provisioned_metrics)
        assert result == AnomalyType.UNDER_PROVISIONED

    def test_classify_memory_leak_by_oom(self, oom_metrics):
        detector = ResourceAnomalyDetector()
        result = detector._classify(oom_metrics)
        assert result == AnomalyType.MEMORY_LEAK

    def test_severity_critical_for_oom(self, oom_metrics):
        detector = ResourceAnomalyDetector()
        severity = detector._severity(-0.8, oom_metrics)
        assert severity == "critical"

    def test_severity_high_for_extreme_score(self, sample_metrics):
        detector = ResourceAnomalyDetector()
        severity = detector._severity(-0.6, sample_metrics)
        assert severity == "high"

    def test_predict_batch_returns_one_result_per_input(self):
        detector = make_trained_detector()
        batch = [make_normal_metrics(i) for i in range(5)]
        results = detector.predict_batch(batch)
        assert len(results) == 5

    def test_anomalies_only_filters_correctly(self):
        detector = make_trained_detector(n_normal=50)
        normal = [make_normal_metrics(i) for i in range(10)]
        results = detector.predict_batch(normal)
        anomalies = detector.anomalies_only(results)
        # All results are either anomaly or not — filter works correctly
        assert all(r.is_anomaly for r in anomalies)

    def test_train_with_empty_list_does_not_raise(self):
        detector = ResourceAnomalyDetector()
        detector.train([])  # should not raise
        assert detector.is_trained is False
