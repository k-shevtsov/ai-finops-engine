# src/notifier.py
# Telegram notifications for FinOps recommendations
# Pattern reused from ai-incident-response

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

# Severity → emoji mapping
SEVERITY_EMOJI = {
    "critical": "🔴",
    "high":     "🟠",
    "medium":   "🟡",
    "low":      "🟢",
    "none":     "⚪",
}

ANOMALY_EMOJI = {
    "over_provisioned":  "💸",
    "under_provisioned": "⚡",
    "memory_leak":       "🧠",
    "cpu_spike":         "📈",
}

MODE_EMOJI = {
    "AUTO":    "🤖",
    "SUGGEST": "💡",
    "MANUAL":  "👤",
}


class TelegramNotifier:
    """Send FinOps recommendation notifications to Telegram."""

    def __init__(
        self,
        token: Optional[str] = None,
        chat_id: Optional[str] = None,
        timeout: int = 10,
    ):
        self.token = token if token is not None else os.environ.get("TELEGRAM_TOKEN", "")
        self.chat_id = chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID", "")
        self.timeout = timeout
        self._sent_ids: set[str] = set()  # deduplication by recommendation name

    def notify_recommendation(
        self,
        name: str,
        namespace: str,
        deployment: str,
        anomaly_type: str,
        severity: str,
        mode: str,
        saving_usd: float,
        confidence: float,
        risk: str,
        root_cause: str,
        recommended: dict,
        applied: bool = False,
    ) -> bool:
        """Send recommendation notification. Returns True on success."""
        if not self._is_configured():
            logger.debug("Telegram not configured — skipping notification")
            return False

        # Deduplicate
        if name in self._sent_ids:
            logger.debug("Notification already sent for %s — skipping", name)
            return False

        message = self._format_message(
            name=name,
            namespace=namespace,
            deployment=deployment,
            anomaly_type=anomaly_type,
            severity=severity,
            mode=mode,
            saving_usd=saving_usd,
            confidence=confidence,
            risk=risk,
            root_cause=root_cause,
            recommended=recommended,
            applied=applied,
        )

        success = self._send(message)
        if success:
            self._sent_ids.add(name)
        return success

    def notify_error(self, component: str, error: str) -> bool:
        """Send error notification."""
        if not self._is_configured():
            return False

        message = (
            f"🚨 *AI FinOps Engine — Error*\n\n"
            f"Component: `{component}`\n"
            f"Error: `{error}`\n"
            f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )
        return self._send(message)

    def _format_message(
        self,
        name: str,
        namespace: str,
        deployment: str,
        anomaly_type: str,
        severity: str,
        mode: str,
        saving_usd: float,
        confidence: float,
        risk: str,
        root_cause: str,
        recommended: dict,
        applied: bool,
    ) -> str:
        sev_emoji = SEVERITY_EMOJI.get(severity, "⚪")
        anom_emoji = ANOMALY_EMOJI.get(anomaly_type, "🔍")
        mode_emoji = MODE_EMOJI.get(mode, "")
        status = "✅ *Applied*" if applied else f"{mode_emoji} *{mode} mode*"

        return (
            f"{sev_emoji} *FinOps Recommendation* {anom_emoji}\n\n"
            f"*Deployment:* `{namespace}/{deployment}`\n"
            f"*Type:* {anomaly_type.replace('_', ' ').title()}\n"
            f"*Severity:* {severity} {sev_emoji}\n"
            f"*Status:* {status}\n\n"
            f"*Root cause:* {root_cause}\n\n"
            f"*Recommended resources:*\n"
            f"  CPU: `{recommended.get('cpu_request', '?')}` → `{recommended.get('cpu_limit', '?')}`\n"
            f"  Memory: `{recommended.get('memory_request', '?')}` → `{recommended.get('memory_limit', '?')}`\n\n"
            f"💰 *Saving:* ${saving_usd:.2f}/month\n"
            f"🎯 *Confidence:* {confidence:.0%}\n"
            f"⚠️ *Risk:* {risk}\n\n"
            f"📋 CRD: `{name}`\n"
            f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )

    def _send(self, message: str) -> bool:
        """Send message to Telegram API."""
        url = TELEGRAM_API.format(token=self.token)
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }

        try:
            response = requests.post(url, json=payload, timeout=self.timeout)
            response.raise_for_status()
            return True
        except requests.exceptions.ConnectionError:
            logger.error("Cannot reach Telegram API")
            return False
        except requests.exceptions.Timeout:
            logger.error("Telegram API request timed out")
            return False
        except requests.exceptions.HTTPError as e:
            logger.error("Telegram API error: %s", e)
            return False

    def _is_configured(self) -> bool:
        return bool(self.token and self.chat_id)
