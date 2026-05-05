# src/model.py
# Isolation Forest anomaly detector for Kubernetes resource waste
# Adapted from aiops-anomaly-detector pattern

import logging
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

from src.collector import ContainerMetrics

logger = logging.getLogger(__name__)

# Minimum samples required before training
MIN_TRAINING_SAMPLES = 10

# Isolation Forest hyperparameters
CONTAMINATION = 0.1  # expected fraction of anomalies
N_ESTIMATORS = 100
RANDOM_STATE = 42

# Anomaly score threshold — scores below this are anomalies
# IsolationForest returns negative scores for outliers
ANOMALY_SCORE_THRESHOLD = -0.1


class AnomalyType(str, Enum):
    OVER_PROVISIONED = "over_provisioned"
    UNDER_PROVISIONED = "under_provisioned"
    MEMORY_LEAK = "memory_leak"
    CPU_SPIKE = "cpu_spike"
    NORMAL = "normal"


@dataclass
class AnomalyResult:
    """Result of anomaly detection for a single container."""

    namespace: str
    deployment: str
    container: str

    anomaly_score: float  # raw Isolation Forest score (negative = anomaly)
    is_anomaly: bool
    anomaly_type: AnomalyType

    # Feature values used for detection
    cpu_utilization: float
    memory_utilization: float
    cpu_limit_ratio: float
    memory_limit_ratio: float
    cpu_throttling_rate: float
    oom_events_24h: int

    # Human-readable severity
    severity: str = "none"  # none | low | medium | high | critical


class ResourceAnomalyDetector:
    """
    Isolation Forest based detector for Kubernetes resource anomalies.

    Detects:
    - OVER_PROVISIONED:   utilization < threshold, large waste
    - UNDER_PROVISIONED:  throttling > threshold or memory overuse
    - MEMORY_LEAK:        memory utilization > request (growing pattern)
    - CPU_SPIKE:          cpu_limit_ratio too low relative to usage variance
    """

    def __init__(
        self,
        contamination: float = CONTAMINATION,
        n_estimators: int = N_ESTIMATORS,
        min_samples: int = MIN_TRAINING_SAMPLES,
        history_size: int = 1000,
    ):
        self.contamination = contamination
        self.n_estimators = n_estimators
        self.min_samples = min_samples

        self._model: Optional[IsolationForest] = None
        self._scaler: Optional[StandardScaler] = None
        self._is_trained = False

        # Rolling buffer of feature vectors for incremental retraining
        self._history: deque = deque(maxlen=history_size)

    @property
    def is_trained(self) -> bool:
        return self._is_trained

    def _extract_features(self, metrics: ContainerMetrics) -> np.ndarray:
        """Extract 6-dimensional feature vector from ContainerMetrics.

        Features:
          0: cpu_utilization       — actual / request (< 0.1 = wasteful)
          1: memory_utilization    — actual / request (< 0.1 = wasteful)
          2: cpu_limit_ratio       — limit / request  (> 10 = suspicious)
          3: memory_limit_ratio    — limit / request  (> 4  = suspicious)
          4: cpu_throttling_rate   — throttled / total (> 0.1 = under-provisioned)
          5: oom_events_24h        — OOM kill count (> 0 = critical)
        """
        return np.array(
            [
                metrics.cpu_utilization,
                metrics.memory_utilization,
                metrics.cpu_limit_ratio,
                metrics.memory_limit_ratio,
                metrics.cpu_throttling_rate,
                float(metrics.oom_events_24h),
            ],
            dtype=np.float64,
        )

    def train(self, metrics_list: list[ContainerMetrics]) -> None:
        """Train Isolation Forest on a batch of container metrics.

        Adds samples to history buffer and retrains from scratch.
        Requires at least min_samples data points.
        """
        if not metrics_list:
            logger.warning("Training called with empty metrics list — skipping")
            return

        for m in metrics_list:
            self._history.append(self._extract_features(m))

        if len(self._history) < self.min_samples:
            logger.info(
                "Not enough samples to train: %d / %d",
                len(self._history),
                self.min_samples,
            )
            return

        X = np.array(list(self._history))

        self._scaler = StandardScaler()
        X_scaled = self._scaler.fit_transform(X)

        self._model = IsolationForest(
            contamination=self.contamination,
            n_estimators=self.n_estimators,
            random_state=RANDOM_STATE,
            n_jobs=-1,
        )
        self._model.fit(X_scaled)
        self._is_trained = True

        logger.info(
            "Isolation Forest trained on %d samples (%d containers)",
            len(self._history),
            len(metrics_list),
        )

    def predict(self, metrics: ContainerMetrics) -> AnomalyResult:
        """Predict anomaly for a single container.

        Returns AnomalyResult with is_anomaly=False if model not trained yet.
        """
        features = self._extract_features(metrics)

        if not self._is_trained:
            return AnomalyResult(
                namespace=metrics.namespace,
                deployment=metrics.deployment,
                container=metrics.container,
                anomaly_score=0.0,
                is_anomaly=False,
                anomaly_type=AnomalyType.NORMAL,
                cpu_utilization=metrics.cpu_utilization,
                memory_utilization=metrics.memory_utilization,
                cpu_limit_ratio=metrics.cpu_limit_ratio,
                memory_limit_ratio=metrics.memory_limit_ratio,
                cpu_throttling_rate=metrics.cpu_throttling_rate,
                oom_events_24h=metrics.oom_events_24h,
                severity="none",
            )

        X = features.reshape(1, -1)
        X_scaled = self._scaler.transform(X)

        # score_samples returns negative values for outliers
        score = float(self._model.score_samples(X_scaled)[0])
        is_anomaly = score < ANOMALY_SCORE_THRESHOLD

        anomaly_type = self._classify(metrics) if is_anomaly else AnomalyType.NORMAL
        severity = self._severity(score, metrics) if is_anomaly else "none"

        return AnomalyResult(
            namespace=metrics.namespace,
            deployment=metrics.deployment,
            container=metrics.container,
            anomaly_score=round(score, 4),
            is_anomaly=is_anomaly,
            anomaly_type=anomaly_type,
            cpu_utilization=metrics.cpu_utilization,
            memory_utilization=metrics.memory_utilization,
            cpu_limit_ratio=metrics.cpu_limit_ratio,
            memory_limit_ratio=metrics.memory_limit_ratio,
            cpu_throttling_rate=metrics.cpu_throttling_rate,
            oom_events_24h=metrics.oom_events_24h,
            severity=severity,
        )

    def predict_batch(
        self, metrics_list: list[ContainerMetrics]
    ) -> list[AnomalyResult]:
        """Predict anomalies for a list of containers."""
        return [self.predict(m) for m in metrics_list]

    def anomalies_only(self, results: list[AnomalyResult]) -> list[AnomalyResult]:
        """Filter results to only anomalous containers."""
        return [r for r in results if r.is_anomaly]

    def _classify(self, metrics: ContainerMetrics) -> AnomalyType:
        """Rule-based classification of anomaly type.

        Called only when Isolation Forest already flagged the container.
        Rules are ordered by severity — first match wins.
        """
        # OOM kills → memory leak / under-provisioned
        if metrics.oom_events_24h > 0:
            return AnomalyType.MEMORY_LEAK

        # High throttling → under-provisioned CPU
        if metrics.cpu_throttling_rate > 0.10:
            return AnomalyType.UNDER_PROVISIONED

        # Memory utilization above request → potential leak
        if metrics.memory_utilization > 1.0:
            return AnomalyType.MEMORY_LEAK

        # CPU utilization above request but limit too low → spike risk
        if metrics.cpu_utilization > 0.8 and metrics.cpu_limit_ratio < 2.0:
            return AnomalyType.CPU_SPIKE

        # Default: over-provisioned (most common case)
        return AnomalyType.OVER_PROVISIONED

    def _severity(self, score: float, metrics: ContainerMetrics) -> str:
        """Map anomaly score + metrics to human-readable severity."""
        # OOM or extreme throttling → always critical
        if metrics.oom_events_24h > 0 or metrics.cpu_throttling_rate > 0.5:
            return "critical"

        # Score-based severity for over-provisioning
        if score < -0.5:
            return "high"
        if score < -0.3:
            return "medium"
        return "low"
