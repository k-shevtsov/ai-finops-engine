# tests/test_operator.py
# Unit tests for src/operator.py
# All Kubernetes API calls are mocked — no real cluster required

from unittest.mock import MagicMock, call, patch

import kubernetes

import pytest

import src.operator as operator_module
from src.operator import (
    _handle_auto_mode,
    _handle_manual_mode,
    _handle_suggest_mode,
    apply_rightsizing,
    handle_recommendation_created,
)


def make_spec(
    mode: str = "AUTO",
    confidence: float = 0.91,
    risk: str = "low",
    saving: float = 11.20,
    anomaly_type: str = "over_provisioned",
    severity: str = "high",
    namespace: str = "default",
    deployment: str = "waste-demo-1",
) -> dict:
    return {
        "deployment": deployment,
        "namespace": namespace,
        "anomaly_type": anomaly_type,
        "severity": severity,
        "mode": mode,
        "recommended": {
            "cpu_request": "20m",
            "cpu_limit": "100m",
            "memory_request": "40Mi",
            "memory_limit": "128Mi",
        },
        "analysis": {
            "confidence": confidence,
            "risk": risk,
            "monthly_saving_usd": saving,
            "monthly_waste_usd": 12.50,
            "explanation": "CPU limit 46x higher than p95 usage",
        },
    }


@pytest.fixture(autouse=True)
def reset_env(monkeypatch):
    """Reset operator env vars to known defaults for each test."""
    monkeypatch.setattr(operator_module, "OPERATOR_MODE", "AUTO")
    monkeypatch.setattr(operator_module, "DRY_RUN", False)
    monkeypatch.setattr(operator_module, "CONFIDENCE_THRESHOLD", 0.85)
    monkeypatch.setattr(operator_module, "MIN_SAVING_USD", 5.0)
    monkeypatch.setattr(
        operator_module, "EXCLUDED_NAMESPACES",
        {"kube-system", "monitoring", "argocd"}
    )


class TestHandleRecommendationCreated:

    @patch("src.operator._update_status")
    @patch("src.operator.apply_rightsizing", return_value=True)
    def test_auto_mode_applies_when_criteria_met(self, mock_apply, mock_status):
        spec = make_spec(mode="AUTO", confidence=0.91, risk="low", saving=11.20)
        handle_recommendation_created(
            spec=spec, name="test-rec", namespace="default"
        )
        mock_apply.assert_called_once()

    @patch("src.operator._update_status")
    @patch("src.operator.apply_rightsizing", return_value=True)
    def test_auto_mode_skips_when_confidence_too_low(self, mock_apply, mock_status):
        spec = make_spec(mode="AUTO", confidence=0.70, risk="low", saving=11.20)
        handle_recommendation_created(
            spec=spec, name="test-rec", namespace="default"
        )
        mock_apply.assert_not_called()

    @patch("src.operator._update_status")
    @patch("src.operator.apply_rightsizing", return_value=True)
    def test_auto_mode_skips_when_risk_high(self, mock_apply, mock_status):
        spec = make_spec(mode="AUTO", confidence=0.95, risk="high", saving=11.20)
        handle_recommendation_created(
            spec=spec, name="test-rec", namespace="default"
        )
        mock_apply.assert_not_called()

    @patch("src.operator._update_status")
    @patch("src.operator.apply_rightsizing", return_value=True)
    def test_auto_mode_skips_when_saving_too_low(self, mock_apply, mock_status):
        spec = make_spec(mode="AUTO", confidence=0.95, risk="low", saving=2.00)
        handle_recommendation_created(
            spec=spec, name="test-rec", namespace="default"
        )
        mock_apply.assert_not_called()

    @patch("src.operator._update_status")
    @patch("src.operator._handle_suggest_mode")
    def test_suggest_mode_does_not_apply(self, mock_suggest, mock_status):
        spec = make_spec(mode="SUGGEST")
        handle_recommendation_created(
            spec=spec, name="test-rec", namespace="default"
        )
        mock_suggest.assert_called_once()

    @patch("src.operator._update_status")
    @patch("src.operator._handle_manual_mode")
    def test_manual_mode_notifies_only(self, mock_manual, mock_status):
        spec = make_spec(mode="MANUAL")
        handle_recommendation_created(
            spec=spec, name="test-rec", namespace="default"
        )
        mock_manual.assert_called_once()

    @patch("src.operator._update_status")
    @patch("src.operator.apply_rightsizing")
    def test_excluded_namespace_skips_all_actions(self, mock_apply, mock_status):
        spec = make_spec(namespace="kube-system")
        # Override target namespace in spec
        spec["namespace"] = "kube-system"
        handle_recommendation_created(
            spec=spec, name="test-rec", namespace="kube-system"
        )
        mock_apply.assert_not_called()
        mock_status.assert_called_with("test-rec", "kube-system", "Rejected", "Namespace excluded")

    @patch("src.operator._update_status")
    @patch("src.operator.apply_rightsizing", return_value=True)
    def test_dry_run_does_not_call_apply(self, mock_apply, mock_status, monkeypatch):
        monkeypatch.setattr(operator_module, "DRY_RUN", True)
        spec = make_spec(mode="AUTO", confidence=0.95, risk="low", saving=11.20)
        handle_recommendation_created(
            spec=spec, name="test-rec", namespace="default"
        )
        mock_apply.assert_not_called()

    @patch("src.operator._update_status")
    @patch("src.operator.apply_rightsizing", return_value=False)
    def test_failed_apply_sets_rejected_status(self, mock_apply, mock_status):
        spec = make_spec(mode="AUTO", confidence=0.95, risk="low", saving=11.20)
        handle_recommendation_created(
            spec=spec, name="test-rec", namespace="default"
        )
        mock_status.assert_called_with("test-rec", "default", "Rejected", "kubectl patch failed")


class TestApplyRightsizing:

    @patch("src.operator.k8s_client.AppsV1Api")
    @patch("src.operator.kubernetes.config.load_kube_config")
    @patch("src.operator.kubernetes.config.load_incluster_config",
           side_effect=kubernetes.config.ConfigException)
    def test_apply_rightsizing_calls_patch(
        self, mock_incluster, mock_kube_cfg, mock_apps_cls
    ):
        mock_api = MagicMock()
        mock_apps_cls.return_value = mock_api

        result = apply_rightsizing(
            namespace="default",
            deployment="waste-demo-1",
            recommended={
                "cpu_request": "20m",
                "cpu_limit": "100m",
                "memory_request": "40Mi",
                "memory_limit": "128Mi",
            },
        )

        assert result is True
        mock_api.patch_namespaced_deployment.assert_called_once()

    @patch("src.operator.k8s_client.AppsV1Api")
    @patch("src.operator.kubernetes.config.load_kube_config")
    @patch("src.operator.kubernetes.config.load_incluster_config",
           side_effect=kubernetes.config.ConfigException)
    def test_apply_rightsizing_returns_false_on_api_exception(
        self, mock_incluster, mock_kube_cfg, mock_apps_cls
    ):
        from kubernetes.client.exceptions import ApiException

        mock_api = MagicMock()
        mock_api.patch_namespaced_deployment.side_effect = ApiException(status=403)
        mock_apps_cls.return_value = mock_api

        result = apply_rightsizing(
            namespace="default",
            deployment="waste-demo-1",
            recommended={"cpu_request": "20m", "cpu_limit": "100m",
                         "memory_request": "40Mi", "memory_limit": "128Mi"},
        )
        assert result is False

    @patch("src.operator.kubernetes.config.load_kube_config", side_effect=Exception)
    @patch("src.operator.kubernetes.config.load_incluster_config",
           side_effect=kubernetes.config.ConfigException)
    def test_apply_rightsizing_returns_false_when_kubeconfig_missing(
        self, mock_incluster, mock_kube_cfg
    ):
        result = apply_rightsizing(
            namespace="default",
            deployment="svc",
            recommended={},
        )
        assert result is False


class TestSuggestMode:

    @patch("src.operator._update_status")
    def test_suggest_mode_updates_status_to_pending(self, mock_status):
        spec = make_spec()
        _handle_suggest_mode(spec, "rec-1", "default", "over_provisioned", "high")
        mock_status.assert_called_once()
        args = mock_status.call_args[0]
        assert args[2] == "Pending"


class TestUpdateStatus:

    @patch("src.operator.kubernetes.config.load_incluster_config",
           side_effect=kubernetes.config.ConfigException)
    @patch("src.operator.kubernetes.config.load_kube_config", side_effect=Exception)
    def test_update_status_does_not_raise_when_kubeconfig_missing(
        self, mock_kube, mock_incluster
    ):
        # Should log debug and return gracefully — never raise
        from src.operator import _update_status
        _update_status("rec-1", "default", "Applied", "test")


class TestHandleRecommendationUpdated:

    def test_updated_handler_does_not_raise(self):
        from src.operator import handle_recommendation_updated
        handle_recommendation_updated(
            spec={}, name="rec-1", namespace="default",
            old={}, new={}
        )
