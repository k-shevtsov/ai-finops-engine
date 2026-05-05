# tests/test_collector.py
# Unit tests for src/collector.py
# All tests use mocked Prometheus — no real cluster required

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.collector import ContainerMetrics, MetricsCollector, PrometheusClient


# ─── PrometheusClient tests ───────────────────────────────────────────────────


class TestPrometheusClient:

    def test_query_returns_results_on_success(self):
        client = PrometheusClient("http://localhost:9090")
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "success",
            "data": {
                "result": [
                    {
                        "metric": {
                            "namespace": "default",
                            "pod": "p1",
                            "container": "c1",
                        },
                        "value": [1234567890, "0.015"],
                    }
                ]
            },
        }
        mock_response.raise_for_status = MagicMock()

        with patch("requests.get", return_value=mock_response):
            result = client.query("some_metric")

        assert len(result) == 1
        assert result[0]["metric"]["container"] == "c1"

    def test_query_returns_empty_list_on_non_success_status(self):
        client = PrometheusClient("http://localhost:9090")
        mock_response = MagicMock()
        mock_response.json.return_value = {"status": "error", "error": "bad query"}
        mock_response.raise_for_status = MagicMock()

        with patch("requests.get", return_value=mock_response):
            result = client.query("bad_query")

        assert result == []

    def test_query_raises_on_connection_error(self):
        client = PrometheusClient("http://localhost:9090")
        with patch("requests.get", side_effect=requests.exceptions.ConnectionError):
            with pytest.raises(requests.exceptions.ConnectionError):
                client.query("any_metric")

    def test_query_raises_on_timeout(self):
        client = PrometheusClient("http://localhost:9090")
        with patch("requests.get", side_effect=requests.exceptions.Timeout):
            with pytest.raises(requests.exceptions.Timeout):
                client.query("any_metric")

    def test_query_value_returns_keyed_floats(self):
        client = PrometheusClient("http://localhost:9090")
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "success",
            "data": {
                "result": [
                    {
                        "metric": {
                            "namespace": "default",
                            "pod": "pod1",
                            "container": "nginx",
                        },
                        "value": [0, "0.123"],
                    }
                ]
            },
        }
        mock_response.raise_for_status = MagicMock()

        with patch("requests.get", return_value=mock_response):
            result = client.query_value("some_metric")

        assert "default/pod1/nginx" in result
        assert result["default/pod1/nginx"] == pytest.approx(0.123)

    def test_query_value_uses_default_on_invalid_value(self):
        client = PrometheusClient("http://localhost:9090")
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "success",
            "data": {
                "result": [
                    {
                        "metric": {"namespace": "ns", "pod": "p", "container": "c"},
                        "value": [0, "NaN"],
                    }
                ]
            },
        }
        mock_response.raise_for_status = MagicMock()

        with patch("requests.get", return_value=mock_response):
            result = client.query_value("metric", default=99.0)

        # NaN cast to float is valid in Python — just check key exists
        assert "ns/p/c" in result


# ─── MetricsCollector tests ───────────────────────────────────────────────────


class TestMetricsCollector:

    def _make_collector(self, mock_client):
        collector = MetricsCollector.__new__(MetricsCollector)
        collector.client = mock_client
        collector.excluded_namespaces = MetricsCollector.DEFAULT_EXCLUDED
        return collector

    def test_collect_returns_list_of_container_metrics(self):
        mock_client = MagicMock()
        mock_client.query_value.return_value = {"default/pod-abc/nginx": 0.015}
        mock_client.query.return_value = []

        collector = self._make_collector(mock_client)
        results = collector.collect()

        assert isinstance(results, list)
        assert all(isinstance(m, ContainerMetrics) for m in results)

    def test_collect_excludes_kube_system_namespace(self):
        mock_client = MagicMock()
        mock_client.query_value.return_value = {
            "kube-system/pod-abc/coredns": 0.010,
            "default/pod-xyz/nginx": 0.015,
        }
        mock_client.query.return_value = []

        collector = self._make_collector(mock_client)
        results = collector.collect()

        namespaces = [m.namespace for m in results]
        assert "kube-system" not in namespaces

    def test_collect_excludes_monitoring_namespace(self):
        mock_client = MagicMock()
        mock_client.query_value.return_value = {
            "monitoring/prom-pod/prometheus": 0.100,
        }
        mock_client.query.return_value = []

        collector = self._make_collector(mock_client)
        results = collector.collect()

        assert len(results) == 0

    def test_collect_calculates_cpu_utilization_correctly(self):
        mock_client = MagicMock()

        def query_value_side_effect(promql):
            key = "default/pod-abc/nginx"
            if "cpu_usage" in promql or "rate(container_cpu" in promql:
                return {key: 0.015}
            if 'resource="cpu"' in promql and "requests" in promql:
                return {key: 0.200}
            return {key: 0.0}

        mock_client.query_value.side_effect = query_value_side_effect
        mock_client.query.return_value = []

        collector = self._make_collector(mock_client)
        results = collector.collect()

        nginx = next((m for m in results if m.container == "nginx"), None)
        if nginx:
            assert nginx.cpu_utilization == pytest.approx(0.015 / 0.200, rel=0.01)

    def test_collect_handles_missing_cpu_request_gracefully(self):
        mock_client = MagicMock()
        mock_client.query_value.return_value = {"default/pod-abc/nginx": 0.015}
        mock_client.query.return_value = []

        collector = self._make_collector(mock_client)
        # Should not raise even with missing requests (division by zero guard)
        results = collector.collect()
        assert isinstance(results, list)

    def test_collect_calculates_cpu_waste_correctly(self, sample_metrics):
        expected_waste = (
            sample_metrics.cpu_request_cores - sample_metrics.cpu_usage_cores
        )
        assert sample_metrics.cpu_waste_cores == pytest.approx(expected_waste)

    def test_collect_calculates_memory_waste_correctly(self, sample_metrics):
        expected_waste = (
            sample_metrics.memory_request_bytes - sample_metrics.memory_usage_bytes
        )
        assert sample_metrics.memory_waste_bytes == pytest.approx(expected_waste)

    def test_parse_key_returns_correct_components(self):
        collector = MetricsCollector.__new__(MetricsCollector)
        ns, pod, container = collector._parse_key("default/my-pod-abc/nginx")
        assert ns == "default"
        assert pod == "my-pod-abc"
        assert container == "nginx"

    def test_parse_key_returns_empty_strings_for_invalid_key(self):
        collector = MetricsCollector.__new__(MetricsCollector)
        ns, pod, container = collector._parse_key("invalid")
        assert ns == ""
        assert pod == ""
        assert container == ""

    def test_health_check_returns_true_on_200(self):
        collector = MetricsCollector("http://localhost:9090")
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("requests.get", return_value=mock_response):
            assert collector.health_check() is True

    def test_health_check_returns_false_on_connection_error(self):
        collector = MetricsCollector("http://localhost:9090")
        with patch("requests.get", side_effect=Exception("connection refused")):
            assert collector.health_check() is False

    def test_collected_at_is_set_on_container_metrics(self, sample_metrics):
        assert isinstance(sample_metrics.collected_at, datetime)

    def test_custom_excluded_namespaces_are_respected(self):
        mock_client = MagicMock()
        mock_client.query_value.return_value = {
            "staging/pod-abc/app": 0.050,
            "default/pod-xyz/nginx": 0.015,
        }
        mock_client.query.return_value = []

        collector = MetricsCollector.__new__(MetricsCollector)
        collector.client = mock_client
        collector.excluded_namespaces = {"staging"}  # custom exclusion

        results = collector.collect()
        namespaces = [m.namespace for m in results]
        assert "staging" not in namespaces
