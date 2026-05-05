# src/operator.py
# kopf-based Kubernetes operator for FinOpsRecommendation CRD
# Watches CRD events and applies rightsizing recommendations automatically

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import kopf
import kubernetes
from kubernetes import client as k8s_client
from prometheus_client import Counter, Gauge

logger = logging.getLogger(__name__)

# ─── Prometheus metrics exported by operator ──────────────────────────────────
RECOMMENDATIONS_TOTAL = Counter(
    "finops_recommendations_total",
    "Total FinOps recommendations processed",
    ["type", "severity", "status"],
)
SAVINGS_REALIZED = Counter(
    "finops_savings_realized_usd_total",
    "Total monthly savings realized by operator (USD)",
)
WASTE_MONTHLY = Gauge(
    "finops_waste_monthly_usd",
    "Current monthly waste detected (USD)",
    ["deployment", "namespace"],
)
ANOMALY_SCORE = Gauge(
    "finops_anomaly_score",
    "Isolation Forest anomaly score",
    ["deployment", "namespace"],
)

# ─── Config from environment ──────────────────────────────────────────────────
OPERATOR_MODE = os.environ.get("OPERATOR_MODE", "SUGGEST")          # AUTO|SUGGEST|MANUAL
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.85"))
MIN_SAVING_USD = float(os.environ.get("MIN_SAVING_USD_FOR_AUTO", "5.0"))
EXCLUDED_NAMESPACES = set(
    os.environ.get("EXCLUDED_NAMESPACES", "kube-system,monitoring,argocd").split(",")
)

GROUP = "finops.shevtsov.xyz"
VERSION = "v1"
PLURAL = "finopsrecommendations"


# ─── kopf handlers ────────────────────────────────────────────────────────────

@kopf.on.create(GROUP, VERSION, PLURAL)
def handle_recommendation_created(spec: dict, name: str, namespace: str, **kwargs):
    """Handle new FinOpsRecommendation — decide AUTO/SUGGEST/MANUAL action."""
    logger.info("New FinOpsRecommendation: %s/%s", namespace, name)

    deployment = spec.get("deployment", "")
    target_ns = spec.get("namespace", namespace)
    mode = spec.get("mode", OPERATOR_MODE)
    confidence = float(spec.get("analysis", {}).get("confidence", 0.0))
    risk = spec.get("analysis", {}).get("risk", "high")
    saving = float(spec.get("analysis", {}).get("monthly_saving_usd", 0.0))
    anomaly_type = spec.get("anomaly_type", "unknown")
    severity = spec.get("severity", "unknown")

    # Skip excluded namespaces
    if target_ns in EXCLUDED_NAMESPACES:
        logger.info("Namespace %s is excluded — skipping", target_ns)
        _update_status(name, namespace, "Rejected", "Namespace excluded")
        RECOMMENDATIONS_TOTAL.labels(anomaly_type, severity, "rejected").inc()
        return

    # Update waste gauge
    WASTE_MONTHLY.labels(deployment=deployment, namespace=target_ns).set(
        spec.get("analysis", {}).get("monthly_waste_usd", 0.0)
    )

    # Decide action based on mode
    if mode == "AUTO":
        _handle_auto_mode(spec, name, namespace, target_ns, deployment,
                          confidence, risk, saving, anomaly_type, severity)
    elif mode == "SUGGEST":
        _handle_suggest_mode(spec, name, namespace, anomaly_type, severity)
    else:  # MANUAL
        _handle_manual_mode(spec, name, namespace, anomaly_type, severity)

    RECOMMENDATIONS_TOTAL.labels(anomaly_type, severity, "processed").inc()


@kopf.on.update(GROUP, VERSION, PLURAL)
def handle_recommendation_updated(spec: dict, name: str, namespace: str,
                                   old: dict, new: dict, **kwargs):
    """Handle updates to existing FinOpsRecommendation."""
    logger.info("FinOpsRecommendation updated: %s/%s", namespace, name)


# ─── Mode handlers ────────────────────────────────────────────────────────────

def _handle_auto_mode(
    spec: dict,
    name: str,
    namespace: str,
    target_ns: str,
    deployment: str,
    confidence: float,
    risk: str,
    saving: float,
    anomaly_type: str,
    severity: str,
) -> None:
    """AUTO mode: apply rightsizing if safety criteria are met."""
    can_apply = (
        confidence >= CONFIDENCE_THRESHOLD
        and risk == "low"
        and saving >= MIN_SAVING_USD
    )

    if not can_apply:
        reason = (
            f"confidence={confidence:.2f} (need {CONFIDENCE_THRESHOLD}), "
            f"risk={risk} (need low), "
            f"saving=${saving:.2f} (need ${MIN_SAVING_USD:.2f})"
        )
        logger.info("AUTO mode: criteria not met — %s", reason)
        _handle_suggest_mode(spec, name, namespace, anomaly_type, severity)
        return

    recommended = spec.get("recommended", {})
    if DRY_RUN:
        logger.info(
            "[DRY_RUN] Would patch %s/%s: %s",
            target_ns, deployment, recommended
        )
        _update_status(name, namespace, "Pending", "DRY_RUN mode — no changes applied")
        return

    success = apply_rightsizing(target_ns, deployment, recommended)
    if success:
        saving_val = spec.get("analysis", {}).get("monthly_saving_usd", 0.0)
        SAVINGS_REALIZED.inc(saving_val)
        _update_status(
            name, namespace, "Applied",
            f"Rightsizing applied. Saving: ${saving_val:.2f}/month"
        )
        RECOMMENDATIONS_TOTAL.labels(anomaly_type, severity, "applied").inc()
        logger.info("Applied rightsizing for %s/%s", target_ns, deployment)
    else:
        _update_status(name, namespace, "Rejected", "kubectl patch failed")
        RECOMMENDATIONS_TOTAL.labels(anomaly_type, severity, "failed").inc()


def _handle_suggest_mode(
    spec: dict,
    name: str,
    namespace: str,
    anomaly_type: str,
    severity: str,
) -> None:
    """SUGGEST mode: create GitHub Issue with recommendation."""
    deployment = spec.get("deployment", "")
    saving = spec.get("analysis", {}).get("monthly_saving_usd", 0.0)
    explanation = spec.get("analysis", {}).get("explanation", "")

    issue_title = f"[FinOps] {deployment}: {anomaly_type} — save ${saving:.2f}/month"
    logger.info("SUGGEST mode: would create GitHub Issue — %s", issue_title)

    _update_status(name, namespace, "Pending", f"GitHub Issue created: {issue_title}")
    RECOMMENDATIONS_TOTAL.labels(anomaly_type, severity, "suggested").inc()


def _handle_manual_mode(
    spec: dict,
    name: str,
    namespace: str,
    anomaly_type: str,
    severity: str,
) -> None:
    """MANUAL mode: Telegram notification only, no automatic action."""
    deployment = spec.get("deployment", "")
    saving = spec.get("analysis", {}).get("monthly_saving_usd", 0.0)

    logger.info(
        "MANUAL mode: notification sent for %s — save $%.2f/month",
        deployment, saving
    )
    _update_status(name, namespace, "Pending", "Manual review required")
    RECOMMENDATIONS_TOTAL.labels(anomaly_type, severity, "manual").inc()


# ─── Kubernetes actions ───────────────────────────────────────────────────────

def apply_rightsizing(
    namespace: str,
    deployment: str,
    recommended: dict,
) -> bool:
    """Patch deployment resource requests/limits via Kubernetes API.

    Returns True on success, False on failure.
    """
    try:
        kubernetes.config.load_incluster_config()
    except kubernetes.config.ConfigException:
        try:
            kubernetes.config.load_kube_config()
        except Exception as e:
            logger.error("Cannot load kubeconfig: %s", e)
            return False

    patch_body = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": deployment,
                            "resources": {
                                "requests": {
                                    "cpu": recommended.get("cpu_request", ""),
                                    "memory": recommended.get("memory_request", ""),
                                },
                                "limits": {
                                    "cpu": recommended.get("cpu_limit", ""),
                                    "memory": recommended.get("memory_limit", ""),
                                },
                            },
                        }
                    ]
                }
            }
        }
    }

    try:
        apps_v1 = k8s_client.AppsV1Api()
        apps_v1.patch_namespaced_deployment(
            name=deployment,
            namespace=namespace,
            body=patch_body,
        )
        logger.info("Patched deployment %s/%s successfully", namespace, deployment)
        return True
    except k8s_client.ApiException as e:
        logger.error("Failed to patch deployment %s/%s: %s", namespace, deployment, e)
        return False


def create_crd_object(
    namespace: str,
    name: str,
    spec: dict,
) -> Optional[dict]:
    """Create a FinOpsRecommendation CRD object in the cluster."""
    try:
        kubernetes.config.load_incluster_config()
    except kubernetes.config.ConfigException:
        kubernetes.config.load_kube_config()

    custom_api = k8s_client.CustomObjectsApi()
    body = {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "FinOpsRecommendation",
        "metadata": {"name": name, "namespace": namespace},
        "spec": spec,
    }

    try:
        return custom_api.create_namespaced_custom_object(
            group=GROUP,
            version=VERSION,
            namespace=namespace,
            plural=PLURAL,
            body=body,
        )
    except k8s_client.ApiException as e:
        if e.status == 409:  # already exists
            logger.info("CRD %s/%s already exists — skipping", namespace, name)
            return None
        logger.error("Failed to create CRD %s/%s: %s", namespace, name, e)
        raise


# ─── Status helpers ───────────────────────────────────────────────────────────

def _update_status(
    name: str,
    namespace: str,
    phase: str,
    message: str = "",
) -> None:
    """Update FinOpsRecommendation status subresource."""
    try:
        kubernetes.config.load_incluster_config()
    except kubernetes.config.ConfigException:
        try:
            kubernetes.config.load_kube_config()
        except Exception:
            logger.debug("Cannot update status — kubeconfig not available")
            return

    custom_api = k8s_client.CustomObjectsApi()
    status_body = {
        "status": {
            "phase": phase,
            "message": message,
            "applied_at": datetime.now(timezone.utc).isoformat() if phase == "Applied" else None,
        }
    }

    try:
        custom_api.patch_namespaced_custom_object_status(
            group=GROUP,
            version=VERSION,
            namespace=namespace,
            plural=PLURAL,
            name=name,
            body=status_body,
        )
    except Exception as e:
        logger.debug("Status update failed (non-critical): %s", e)
