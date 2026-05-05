#!/usr/bin/env bash
# scripts/port-forwards.sh
# nohup-based port forwarding

set -euo pipefail

GREEN='\033[0;32m'
NC='\033[0m'
info() { echo -e "${GREEN}[port-forwards]${NC} $*"; }

CLUSTER="finops"
LOG_DIR="/tmp/finops-pf-logs"
mkdir -p "$LOG_DIR"

kubectl config use-context "k3d-${CLUSTER}" >/dev/null

# kill old
pkill -f "port-forward.*9090" 2>/dev/null || true
pkill -f "port-forward.*3001" 2>/dev/null || true
sleep 1

# nohup
nohup kubectl port-forward -n monitoring \
  svc/prometheus-kube-prometheus-prometheus 9090:9090 \
  >"$LOG_DIR/prometheus.log" 2>&1 &

nohup kubectl port-forward -n monitoring \
  svc/prometheus-grafana 3001:80 \
  >"$LOG_DIR/grafana.log" 2>&1 &

sleep 2

# Check
if ss -tlnp | grep -q ':9090'; then
  info "✅ Prometheus: http://localhost:9090"
else
  info "❌ Prometheus port-forward failed — check $LOG_DIR/prometheus.log"
fi

if ss -tlnp | grep -q ':3001'; then
  info "✅ Grafana:    http://localhost:3001  (admin / finops123)"
else
  info "❌ Grafana port-forward failed — check $LOG_DIR/grafana.log"
fi

info "PIDs: $(pgrep -f 'port-forward' | tr '\n' ' ')"
