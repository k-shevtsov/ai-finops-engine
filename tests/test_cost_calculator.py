# tests/test_cost_calculator.py
# Unit tests for src/cost_calculator.py

import pytest

from src.collector import ContainerMetrics
from src.cost_calculator import (
    HOURS_PER_MONTH,
    CostBreakdown,
    CostCalculator,
)


@pytest.fixture
def calculator():
    return CostCalculator(
        cpu_price_per_core_hour=0.048,
        memory_price_per_gb_hour=0.006,
    )


@pytest.fixture
def zero_usage_metrics():
    """Container with requests set but zero actual usage."""
    return ContainerMetrics(
        namespace="default",
        deployment="idle-service",
        container="app",
        pod="idle-service-abc",
        cpu_usage_cores=0.0,
        memory_usage_bytes=0.0,
        cpu_request_cores=0.500,
        cpu_limit_cores=1.000,
        memory_request_bytes=512 * 1024**2,
        memory_limit_bytes=1024 * 1024**2,
        cpu_utilization=0.0,
        memory_utilization=0.0,
        cpu_limit_ratio=2.0,
        memory_limit_ratio=2.0,
    )


class TestCostCalculator:

    def test_calculate_returns_cost_breakdown(self, calculator, sample_metrics):
        result = calculator.calculate(sample_metrics)
        assert isinstance(result, CostBreakdown)

    def test_calculate_cpu_request_cost_correct(self, calculator, sample_metrics):
        # 0.200 cores * 0.048 USD/core/hour * 730 hours = 7.008 USD
        expected = 0.200 * 0.048 * HOURS_PER_MONTH
        result = calculator.calculate(sample_metrics)
        assert result.cpu_request_cost_monthly == pytest.approx(expected, rel=0.001)

    def test_calculate_memory_request_cost_correct(self, calculator, sample_metrics):
        # 256Mi = 0.25 GB * 0.006 * 730 = 1.095 USD
        gb = (256 * 1024**2) / (1024**3)
        expected = gb * 0.006 * HOURS_PER_MONTH
        result = calculator.calculate(sample_metrics)
        assert result.memory_request_cost_monthly == pytest.approx(expected, rel=0.001)

    def test_calculate_waste_is_positive_for_over_provisioned(
        self, calculator, sample_metrics
    ):
        result = calculator.calculate(sample_metrics)
        assert result.cpu_waste_monthly > 0
        assert result.memory_waste_monthly > 0
        assert result.total_waste_monthly > 0

    def test_calculate_waste_is_zero_for_zero_usage(
        self, calculator, zero_usage_metrics
    ):
        # zero usage means maximum waste
        result = calculator.calculate(zero_usage_metrics)
        assert result.total_waste_monthly > 0
        # waste should equal request cost when usage is 0
        assert result.cpu_waste_monthly == pytest.approx(
            result.cpu_request_cost_monthly, rel=0.001
        )

    def test_potential_saving_less_than_total_waste(self, calculator, sample_metrics):
        # Potential saving accounts for 20% headroom — always less than full waste
        result = calculator.calculate(sample_metrics)
        assert result.potential_saving_monthly <= result.total_waste_monthly

    def test_efficiency_score_between_zero_and_one(self, calculator, sample_metrics):
        result = calculator.calculate(sample_metrics)
        assert 0.0 <= result.efficiency_score <= 1.0

    def test_efficiency_score_low_for_over_provisioned(
        self, calculator, sample_metrics
    ):
        result = calculator.calculate(sample_metrics)
        # CPU utilization 7.5%, memory ~11.7% → efficiency well below 0.2
        assert result.efficiency_score < 0.2

    def test_total_waste_sums_all_containers(self, calculator, sample_metrics):
        metrics_list = [sample_metrics, sample_metrics]
        breakdowns = calculator.calculate_batch(metrics_list)
        total = calculator.total_waste(breakdowns)
        single = calculator.calculate(sample_metrics).total_waste_monthly
        assert total == pytest.approx(single * 2, rel=0.001)

    def test_total_potential_saving_sums_all_containers(
        self, calculator, sample_metrics
    ):
        metrics_list = [sample_metrics, sample_metrics]
        breakdowns = calculator.calculate_batch(metrics_list)
        total = calculator.total_potential_saving(breakdowns)
        single = calculator.calculate(sample_metrics).potential_saving_monthly
        assert total == pytest.approx(single * 2, rel=0.001)

    def test_calculate_batch_skips_bad_metrics_without_raising(self, calculator):
        bad = ContainerMetrics(namespace="", deployment="", container="", pod="")
        good = ContainerMetrics(
            namespace="default",
            deployment="svc",
            container="app",
            pod="svc-abc",
            cpu_request_cores=0.1,
            memory_request_bytes=64 * 1024**2,
        )
        results = calculator.calculate_batch([bad, good])
        # bad metrics still produce a CostBreakdown (zeros), no exception
        assert len(results) == 2

    def test_no_negative_waste(self, calculator, under_provisioned_metrics):
        # Under-provisioned: actual > request — waste must be 0, not negative
        result = calculator.calculate(under_provisioned_metrics)
        assert result.cpu_waste_monthly >= 0.0
        assert result.memory_waste_monthly >= 0.0
