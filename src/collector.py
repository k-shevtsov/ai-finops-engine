# src/collector.py
# Prometheus metrics collector for AI FinOps Engine
# Collects resource utilization metrics per namespace/deployment/container

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)


@dataclass
class ContainerMetrics:
    """Resource metrics for a single container."""

    namespace: str
    deployment: str
    container: str
    pod: str

    # Actual usage (from Prometheus)
    cpu_usage_cores: float = 0.0  # current CPU usage in cores
    memory_usage_bytes: float = 0.0  # current memory usage in bytes

    # Resource requests and limits (from kube_pod_container_resource_*)
    cpu_request_cores: float = 0.0
    cpu_limit_cores: float = 0.0
    memory_request_bytes: float = 0.0
    memory_limit_bytes: float = 0.0

    # Derived metrics (calculated by collector)
    cpu_utilization: float = 0.0  # cpu_usage / cpu_request
    memory_utilization: float = 0.0  # memory_usage / memory_request
    cpu_limit_ratio: float = 0.0  # cpu_limit / cpu_request
    memory_limit_ratio: float = 0.0  # memory_limit / memory_request

    # Throttling and OOM
    cpu_throttling_rate: float = 0.0  # throttled_seconds / total_seconds
    oom_events_24h: int = 0

    # Waste metrics
    cpu_waste_cores: float = 0.0  # cpu_request - cpu_usage
    memory_waste_bytes: float = 0.0  # memory_request - memory_usage

    collected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class PrometheusClient:
    """Thin wrapper around Prometheus HTTP API."""

    def __init__(self, url: str, timeout: int = 30):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def query(self, promql: str) -> list[dict]:
        """Execute instant query, return list of {metric, value} dicts."""
        try:
            response = requests.get(
                f"{self.url}/api/v1/query",
                params={"query": promql},
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()

            if data.get("status") != "success":
                logger.warning("Prometheus query failed: %s", data.get("error"))
                return []

            return data.get("data", {}).get("result", [])

        except requests.exceptions.ConnectionError:
            logger.error("Cannot connect to Prometheus at %s", self.url)
            raise
        except requests.exceptions.Timeout:
            logger.error("Prometheus query timed out: %s", promql[:80])
            raise
        except requests.exceptions.HTTPError as e:
            logger.error("Prometheus HTTP error: %s", e)
            raise

    def query_value(self, promql: str, default: float = 0.0) -> dict[str, float]:
        """Execute query, return {label_key: float_value} mapping.

        Label key is built as 'namespace/deployment/container'.
        Returns empty dict if query yields no results.
        """
        results = self.query(promql)
        output = {}
        for item in results:
            metric = item.get("metric", {})
            value_pair = item.get("value", [None, "0"])
            try:
                value = float(value_pair[1])
            except (IndexError, ValueError, TypeError):
                value = default

            namespace = metric.get("namespace", "")
            container = metric.get("container", "")
            pod = metric.get("pod", "")

            key = f"{namespace}/{pod}/{container}"
            output[key] = value

        return output


class MetricsCollector:
    """Collects and aggregates Kubernetes resource metrics from Prometheus."""

    # Namespaces to skip (system/infrastructure)
    DEFAULT_EXCLUDED = {"kube-system", "monitoring", "argocd", "kube-node-lease"}

    def __init__(
        self,
        prometheus_url: str,
        excluded_namespaces: Optional[set[str]] = None,
        timeout: int = 30,
    ):
        self.client = PrometheusClient(prometheus_url, timeout=timeout)
        self.excluded_namespaces = excluded_namespaces or self.DEFAULT_EXCLUDED

    def collect(self) -> list[ContainerMetrics]:
        """Collect metrics for all non-excluded containers.

        Returns list of ContainerMetrics, one per container.
        Containers with missing requests (unset limits) are included but flagged.
        """
        logger.info("Collecting metrics from Prometheus...")

        # --- Actual usage ---
        cpu_usage = self.client.query_value(
            'rate(container_cpu_usage_seconds_total{container!=""}[5m])'
        )
        memory_usage = self.client.query_value(
            'container_memory_working_set_bytes{container!=""}'
        )

        # --- Requests and limits ---
        cpu_requests = self.client.query_value(
            'kube_pod_container_resource_requests{resource="cpu",container!=""}'
        )
        cpu_limits = self.client.query_value(
            'kube_pod_container_resource_limits{resource="cpu",container!=""}'
        )
        memory_requests = self.client.query_value(
            'kube_pod_container_resource_requests{resource="memory",container!=""}'
        )
        memory_limits = self.client.query_value(
            'kube_pod_container_resource_limits{resource="memory",container!=""}'
        )

        # --- Throttling ---
        # Ratio of throttled CPU time to total CPU time
        cpu_throttling = self.client.query_value(
            """
            rate(container_cpu_cfs_throttled_seconds_total{container!=""}[5m])
            /
            rate(container_cpu_cfs_periods_total{container!=""}[5m])
            """
        )

        # --- OOM kills (last 24h) ---
        oom_events = self.client.query_value(
            'increase(kube_pod_container_status_restarts_total{reason="OOMKilled"}[24h])'
        )

        # --- Deployment label mapping ---
        # kube_pod_owner gives us pod → replicaset, then replicaset → deployment
        # Simpler: use kube_pod_labels to get app label as deployment proxy
        pod_to_deployment = self._get_pod_deployment_mapping()

        # --- Build ContainerMetrics objects ---
        all_keys = set(cpu_usage) | set(memory_usage) | set(cpu_requests)
        metrics = []

        for key in all_keys:
            namespace, pod, container = self._parse_key(key)

            if not namespace or not container:
                continue

            if namespace in self.excluded_namespaces:
                continue

            # Skip pause/infra containers
            if container in ("POD", ""):
                continue

            deployment = pod_to_deployment.get(f"{namespace}/{pod}", pod)

            cpu_req = cpu_requests.get(key, 0.0)
            mem_req = memory_requests.get(key, 0.0)
            cpu_lim = cpu_limits.get(key, 0.0)
            mem_lim = memory_limits.get(key, 0.0)
            cpu_use = cpu_usage.get(key, 0.0)
            mem_use = memory_usage.get(key, 0.0)

            # Derived utilization — avoid division by zero
            cpu_util = (cpu_use / cpu_req) if cpu_req > 0 else 0.0
            mem_util = (mem_use / mem_req) if mem_req > 0 else 0.0
            cpu_limit_ratio = (cpu_lim / cpu_req) if cpu_req > 0 else 0.0
            mem_limit_ratio = (mem_lim / mem_req) if mem_req > 0 else 0.0

            # Waste = provisioned but unused
            cpu_waste = max(0.0, cpu_req - cpu_use)
            mem_waste = max(0.0, mem_req - mem_use)

            m = ContainerMetrics(
                namespace=namespace,
                deployment=deployment,
                container=container,
                pod=pod,
                cpu_usage_cores=cpu_use,
                memory_usage_bytes=mem_use,
                cpu_request_cores=cpu_req,
                cpu_limit_cores=cpu_lim,
                memory_request_bytes=mem_req,
                memory_limit_bytes=mem_lim,
                cpu_utilization=cpu_util,
                memory_utilization=mem_util,
                cpu_limit_ratio=cpu_limit_ratio,
                memory_limit_ratio=mem_limit_ratio,
                cpu_throttling_rate=cpu_throttling.get(key, 0.0),
                oom_events_24h=int(oom_events.get(key, 0)),
                cpu_waste_cores=cpu_waste,
                memory_waste_bytes=mem_waste,
            )
            metrics.append(m)

        logger.info("Collected metrics for %d containers", len(metrics))
        return metrics

    def _get_pod_deployment_mapping(self) -> dict[str, str]:
        """Return {namespace/pod: deployment_name} mapping via kube_pod_labels."""
        results = self.client.query('kube_pod_labels{label_app!=""}')
        mapping = {}
        for item in results:
            metric = item.get("metric", {})
            namespace = metric.get("namespace", "")
            pod = metric.get("pod", "")
            # Use label_app as deployment proxy (works for most deployments)
            app = metric.get("label_app", "") or metric.get(
                "label_app_kubernetes_io_name", ""
            )
            if namespace and pod and app:
                mapping[f"{namespace}/{pod}"] = app
        return mapping

    def _parse_key(self, key: str) -> tuple[str, str, str]:
        """Parse 'namespace/pod/container' key into components."""
        parts = key.split("/", 2)
        if len(parts) != 3:
            return "", "", ""
        return parts[0], parts[1], parts[2]

    def health_check(self) -> bool:
        """Check if Prometheus is reachable."""
        try:
            response = requests.get(
                f"{self.client.url}/-/healthy",
                timeout=5,
            )
            return response.status_code == 200
        except Exception:
            return False
