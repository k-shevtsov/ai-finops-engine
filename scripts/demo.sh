#!/usr/bin/env bash
# scripts/demo.sh
# Full AI FinOps Engine demo flow
# Prerequisites: make cluster-up && make inject (wait 5 min for metrics)

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[demo]${NC} $*"; }
step()  { echo -e "\n${CYAN}━━━ $* ━━━${NC}"; }
warn()  { echo -e "${YELLOW}[demo]${NC} $*"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
VENV="$REPO_ROOT/.venv"

cd "$REPO_ROOT"
source "$VENV/bin/activate"
source .env 2>/dev/null || true

kubectl config use-context k3d-finops

step "Step 1: Verify wasteful workloads are running"
kubectl get deployments -n default -l 'app in (waste-demo-1,waste-demo-2,waste-demo-3)' \
  2>/dev/null || {
    warn "Wasteful workloads not found — running inject first..."
    bash scripts/inject-waste.sh
    info "Waiting 60s for initial metrics..."
    sleep 60
}

step "Step 2: Collect metrics from Prometheus"
info "Prometheus: http://localhost:9090"
python3 - << 'PYEOF'
import sys
sys.path.insert(0, '.')
from src.collector import MetricsCollector

collector = MetricsCollector("http://localhost:9090")
if not collector.health_check():
    print("ERROR: Prometheus not reachable — run 'make port-forwards' first")
    sys.exit(1)

metrics = collector.collect()
print(f"Collected metrics for {len(metrics)} containers")

waste = [m for m in metrics if m.cpu_utilization < 0.15 or m.memory_utilization < 0.15]
print(f"Potentially wasteful containers: {len(waste)}")
for m in waste[:5]:
    print(f"  {m.namespace}/{m.deployment}/{m.container}: "
          f"CPU {m.cpu_utilization:.1%}, MEM {m.memory_utilization:.1%}")
PYEOF

step "Step 3: Run Isolation Forest anomaly detection"
python3 - << 'PYEOF'
import sys
sys.path.insert(0, '.')
from src.collector import MetricsCollector
from src.model import ResourceAnomalyDetector

collector = MetricsCollector("http://localhost:9090")
metrics = collector.collect()

if len(metrics) < 5:
    print("Not enough metrics yet — wait 2-3 more minutes for Prometheus to collect data")
    sys.exit(0)

detector = ResourceAnomalyDetector()
detector.train(metrics)
results = detector.predict_batch(metrics)
anomalies = detector.anomalies_only(results)

print(f"Trained on {len(metrics)} containers")
print(f"Anomalies detected: {len(anomalies)}")
for a in anomalies:
    print(f"  [{a.severity.upper()}] {a.namespace}/{a.deployment}: "
          f"{a.anomaly_type} (score={a.anomaly_score:.3f})")
PYEOF

step "Step 4: Generate FinOps recommendations via Claude agent"
python3 - << 'PYEOF'
import sys, os
sys.path.insert(0, '.')
from dotenv import load_dotenv
load_dotenv()

from src.collector import MetricsCollector
from src.model import ResourceAnomalyDetector
from src.agent import FinOpsAgent

collector = MetricsCollector("http://localhost:9090")
metrics = collector.collect()

if not metrics:
    print("No metrics available")
    sys.exit(0)

detector = ResourceAnomalyDetector()
detector.train(metrics)
results = detector.predict_batch(metrics)
anomalies = detector.anomalies_only(results)

if not anomalies:
    print("No anomalies detected — cluster looks healthy!")
    sys.exit(0)

agent = FinOpsAgent()
metrics_by_key = {
    f"{m.namespace}/{m.deployment}/{m.container}": m for m in metrics
}

for anomaly in anomalies[:3]:  # limit to 3 for demo
    key = f"{anomaly.namespace}/{anomaly.deployment}/{anomaly.container}"
    m = metrics_by_key.get(key)
    if not m:
        continue

    print(f"\nAnalyzing {anomaly.deployment}...")
    rec = agent.analyze(m, anomaly)
    print(f"  Type:       {rec.anomaly_type}")
    print(f"  Root cause: {rec.root_cause}")
    print(f"  CPU:        request={rec.cpu_request} limit={rec.cpu_limit}")
    print(f"  Memory:     request={rec.memory_request} limit={rec.memory_limit}")
    print(f"  Saving:     ${rec.monthly_saving_usd:.2f}/month")
    print(f"  Confidence: {rec.confidence:.0%}  Risk: {rec.risk}")
PYEOF

step "Demo complete"
info "✅ AI FinOps Engine demo finished"
info "Open Grafana: http://localhost:3001  (admin / finops123)"
info "Check CRDs:   kubectl get finopsrecommendations -A"
