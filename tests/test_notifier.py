# tests/test_notifier.py
# Unit tests for src/notifier.py

from unittest.mock import MagicMock, patch

import pytest
import requests

from src.notifier import TelegramNotifier


@pytest.fixture
def notifier():
    return TelegramNotifier(token="test-token", chat_id="123456")


@pytest.fixture
def unconfigured_notifier():
    return TelegramNotifier(token="", chat_id="")


SAMPLE_RECOMMENDATION = dict(
    name="waste-demo-1-cpu-waste",
    namespace="default",
    deployment="waste-demo-1",
    anomaly_type="over_provisioned",
    severity="high",
    mode="AUTO",
    saving_usd=11.20,
    confidence=0.91,
    risk="low",
    root_cause="CPU limit is 46x higher than p95 actual usage",
    recommended={
        "cpu_request": "20m",
        "cpu_limit": "100m",
        "memory_request": "40Mi",
        "memory_limit": "128Mi",
    },
    applied=True,
)


class TestTelegramNotifier:

    def test_notify_returns_false_when_not_configured(self, unconfigured_notifier):
        result = unconfigured_notifier.notify_recommendation(**SAMPLE_RECOMMENDATION)
        assert result is False

    def test_notify_returns_true_on_success(self, notifier):
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        with patch("requests.post", return_value=mock_response):
            result = notifier.notify_recommendation(**SAMPLE_RECOMMENDATION)
        assert result is True

    def test_notify_deduplicates_same_recommendation(self, notifier):
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        with patch("requests.post", return_value=mock_response) as mock_post:
            notifier.notify_recommendation(**SAMPLE_RECOMMENDATION)
            notifier.notify_recommendation(**SAMPLE_RECOMMENDATION)
            # Second call should be deduplicated
            assert mock_post.call_count == 1

    def test_notify_returns_false_on_connection_error(self, notifier):
        with patch("requests.post", side_effect=requests.exceptions.ConnectionError):
            result = notifier.notify_recommendation(**SAMPLE_RECOMMENDATION)
        assert result is False

    def test_notify_returns_false_on_timeout(self, notifier):
        with patch("requests.post", side_effect=requests.exceptions.Timeout):
            result = notifier.notify_recommendation(**SAMPLE_RECOMMENDATION)
        assert result is False

    def test_notify_error_sends_message(self, notifier):
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        with patch("requests.post", return_value=mock_response):
            result = notifier.notify_error("collector", "Prometheus unreachable")
        assert result is True

    def test_format_message_contains_deployment_name(self, notifier):
        msg = notifier._format_message(**{
            k: v for k, v in SAMPLE_RECOMMENDATION.items() if k != "name"
        }, name="test-rec")
        assert "waste-demo-1" in msg

    def test_format_message_contains_saving(self, notifier):
        msg = notifier._format_message(**{
            k: v for k, v in SAMPLE_RECOMMENDATION.items() if k != "name"
        }, name="test-rec")
        assert "11.20" in msg

    def test_format_message_shows_applied_status(self, notifier):
        msg = notifier._format_message(**{
            k: v for k, v in SAMPLE_RECOMMENDATION.items() if k != "name"
        }, name="test-rec")
        assert "Applied" in msg

    def test_is_configured_true_when_token_and_chat_set(self, notifier):
        assert notifier._is_configured() is True

    def test_is_configured_false_when_token_missing(self):
        n = TelegramNotifier(token="", chat_id="123")
        assert n._is_configured() is False
