# src/cost_calculator.py
# USD cost calculations for Kubernetes resource waste
# Uses configurable per-unit prices (defaults: approximate AWS us-east-1 on-demand)

import logging
from dataclasses import dataclass

from src.collector import ContainerMetrics

logger = logging.getLogger(__name__)

# Default prices — approximate AWS t3/m5 blended on-demand rates
DEFAULT_CPU_PRICE_PER_CORE_HOUR = 0.048   # USD per core per hour
DEFAULT_MEMORY_PRICE_PER_GB_HOUR = 0.006  # USD per GB per hour

HOURS_PER_MONTH = 730  # 365 * 24 / 12


@dataclass
class CostBreakdown:
    """Cost analysis for a single container."""

    namespace: str
    deployment: str
    container: str

    # Monthly cost of current resource requests
    cpu_request_cost_monthly: float = 0.0
    memory_request_cost_monthly: float = 0.0
    total_request_cost_monthly: float = 0.0

    # Monthly cost of actual usage
    cpu_actual_cost_monthly: float = 0.0
    memory_actual_cost_monthly: float = 0.0
    total_actual_cost_monthly: float = 0.0

    # Waste = request cost - actual cost
    cpu_waste_monthly: float = 0.0
    memory_waste_monthly: float = 0.0
    total_waste_monthly: float = 0.0

    # Potential saving if rightsized to p95 usage * 1.2 headroom
    potential_saving_monthly: float = 0.0

    # Efficiency score: actual / requested (0.0 - 1.0)
    efficiency_score: float = 0.0


class CostCalculator:
    """Calculates USD costs and waste from container resource metrics."""

    def __init__(
        self,
        cpu_price_per_core_hour: float = DEFAULT_CPU_PRICE_PER_CORE_HOUR,
        memory_price_per_gb_hour: float = DEFAULT_MEMORY_PRICE_PER_GB_HOUR,
    ):
        self.cpu_price_per_core_hour = cpu_price_per_core_hour
        self.memory_price_per_gb_hour = memory_price_per_gb_hour

    def calculate(self, metrics: ContainerMetrics) -> CostBreakdown:
        """Calculate cost breakdown for a single container."""

        # CPU costs (cores → USD/month)
        cpu_req_cost = self._cpu_monthly(metrics.cpu_request_cores)
        cpu_actual_cost = self._cpu_monthly(metrics.cpu_usage_cores)
        cpu_waste = max(0.0, cpu_req_cost - cpu_actual_cost)

        # Memory costs (bytes → GB → USD/month)
        mem_req_cost = self._memory_monthly(metrics.memory_request_bytes)
        mem_actual_cost = self._memory_monthly(metrics.memory_usage_bytes)
        mem_waste = max(0.0, mem_req_cost - mem_actual_cost)

        total_req_cost = cpu_req_cost + mem_req_cost
        total_actual_cost = cpu_actual_cost + mem_actual_cost
        total_waste = cpu_waste + mem_waste

        # Potential saving: rightsize requests to actual * 1.2 headroom
        rightsized_cpu = metrics.cpu_usage_cores * 1.2
        rightsized_mem = metrics.memory_usage_bytes * 1.2

        rightsized_cpu_cost = self._cpu_monthly(rightsized_cpu)
        rightsized_mem_cost = self._memory_monthly(rightsized_mem)
        potential_saving = max(
            0.0, total_req_cost - (rightsized_cpu_cost + rightsized_mem_cost)
        )

        # Efficiency score: average of cpu and memory utilization, capped at 1.0
        cpu_eff = min(1.0, metrics.cpu_utilization) if metrics.cpu_request_cores > 0 else 0.0
        mem_eff = min(1.0, metrics.memory_utilization) if metrics.memory_request_bytes > 0 else 0.0

        if metrics.cpu_request_cores > 0 and metrics.memory_request_bytes > 0:
            efficiency_score = (cpu_eff + mem_eff) / 2
        elif metrics.cpu_request_cores > 0:
            efficiency_score = cpu_eff
        elif metrics.memory_request_bytes > 0:
            efficiency_score = mem_eff
        else:
            efficiency_score = 0.0

        return CostBreakdown(
            namespace=metrics.namespace,
            deployment=metrics.deployment,
            container=metrics.container,
            cpu_request_cost_monthly=round(cpu_req_cost, 4),
            memory_request_cost_monthly=round(mem_req_cost, 4),
            total_request_cost_monthly=round(total_req_cost, 4),
            cpu_actual_cost_monthly=round(cpu_actual_cost, 4),
            memory_actual_cost_monthly=round(mem_actual_cost, 4),
            total_actual_cost_monthly=round(total_actual_cost, 4),
            cpu_waste_monthly=round(cpu_waste, 4),
            memory_waste_monthly=round(mem_waste, 4),
            total_waste_monthly=round(total_waste, 4),
            potential_saving_monthly=round(potential_saving, 4),
            efficiency_score=round(efficiency_score, 4),
        )

    def calculate_batch(self, metrics_list: list[ContainerMetrics]) -> list[CostBreakdown]:
        """Calculate cost breakdown for a list of containers."""
        results = []
        for m in metrics_list:
            try:
                results.append(self.calculate(m))
            except Exception as e:
                logger.warning(
                    "Failed to calculate cost for %s/%s: %s",
                    m.namespace, m.container, e
                )
        return results

    def total_waste(self, breakdowns: list[CostBreakdown]) -> float:
        """Sum total monthly waste across all containers."""
        return round(sum(b.total_waste_monthly for b in breakdowns), 2)

    def total_potential_saving(self, breakdowns: list[CostBreakdown]) -> float:
        """Sum potential monthly saving if all containers were rightsized."""
        return round(sum(b.potential_saving_monthly for b in breakdowns), 2)

    def _cpu_monthly(self, cores: float) -> float:
        """Convert CPU cores to monthly USD cost."""
        return cores * self.cpu_price_per_core_hour * HOURS_PER_MONTH

    def _memory_monthly(self, bytes_: float) -> float:
        """Convert memory bytes to monthly USD cost."""
        gb = bytes_ / (1024 ** 3)
        return gb * self.memory_price_per_gb_hour * HOURS_PER_MONTH
