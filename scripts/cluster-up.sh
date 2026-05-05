#!/usr/bin/env bash
# scripts/cluster-up.sh
# Поднимает k3d кластер finops + Prometheus + kube-state-metrics
# Идемпотентный: повторный запуск не ломает уже работающий кластер

set -euo pipefail

CLUSTER_NAME="finops"
NAMESPACE_MONITORING="monitoring"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# ─── Цвета ────────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()    { echo -e "${GREEN}[INFO]${NC} $*"; }
warning() { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# ─── Проверка инструментов ────────────────────────────────────────────────────
check_tools() {
  info "Checking required tools..."
  for tool in k3d kubectl helm; do
    if ! command -v "$tool" &>/dev/null; then
      error "$tool is not installed or not in PATH"
    fi
  done
  info "All tools available ✅"
}

# ─── k3d кластер ──────────────────────────────────────────────────────────────
create_cluster() {
  if k3d cluster list | grep -q "^${CLUSTER_NAME}"; then
    warning "Cluster '${CLUSTER_NAME}' already exists — skipping creation"
    # Убедимся что запущен
    k3d cluster start "${CLUSTER_NAME}" 2>/dev/null || true
  else
    info "Creating k3d cluster '${CLUSTER_NAME}'..."
    k3d cluster create --config "${REPO_ROOT}/infra/k3d/cluster.yaml"
    info "Cluster created ✅"
  fi

  # Переключаем контекст
  kubectl config use-context "k3d-${CLUSTER_NAME}"
  info "kubectl context: k3d-${CLUSTER_NAME}"

  # Ждём nodes Ready
  info "Waiting for nodes to be Ready..."
  kubectl wait --for=condition=Ready nodes --all --timeout=90s
  info "Nodes ready ✅"
}

# ─── Prometheus через kube-prometheus-stack ───────────────────────────────────
install_prometheus() {
  info "Setting up monitoring namespace..."
  kubectl create namespace "${NAMESPACE_MONITORING}" --dry-run=client -o yaml | kubectl apply -f -

  # Добавляем helm repo если нет
  if ! helm repo list | grep -q "prometheus-community"; then
    info "Adding prometheus-community helm repo..."
    helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
  fi
  helm repo update prometheus-community --fail-on-repo-update-fail 2>/dev/null || helm repo update

  # Проверяем — уже установлен?
  if helm status prometheus -n "${NAMESPACE_MONITORING}" &>/dev/null; then
    warning "Prometheus already installed — skipping"
    return 0
  fi

  info "Installing kube-prometheus-stack..."
  helm upgrade --install prometheus prometheus-community/kube-prometheus-stack \
    --namespace "${NAMESPACE_MONITORING}" \
    --set grafana.service.type=LoadBalancer \
    --set grafana.service.port=3000 \
    --set prometheus.service.type=LoadBalancer \
    --set prometheus.prometheusSpec.scrapeInterval=30s \
    --set prometheus.prometheusSpec.evaluationInterval=30s \
    --set alertmanager.enabled=true \
    --set grafana.adminPassword=finops123 \
    --set grafana.defaultDashboardsEnabled=true \
    --wait \
    --timeout=300s

  info "Prometheus stack installed ✅"
}

# ─── Проверка ─────────────────────────────────────────────────────────────────
verify_setup() {
  info "Verifying setup..."

  echo ""
  echo "=== Nodes ==="
  kubectl get nodes -o wide

  echo ""
  echo "=== Monitoring pods ==="
  kubectl get pods -n "${NAMESPACE_MONITORING}"

  echo ""
  echo "=== Services ==="
  kubectl get svc -n "${NAMESPACE_MONITORING}"

  echo ""
  info "✅ Cluster '${CLUSTER_NAME}' is ready!"
  info "Prometheus:  http://localhost:9090"
  info "Grafana:     http://localhost:3001  (admin / finops123)"
  info ""
  info "To forward ports manually:"
  info "  kubectl port-forward -n monitoring svc/prometheus-kube-prometheus-prometheus 9090:9090 &"
  info "  kubectl port-forward -n monitoring svc/prometheus-grafana 3001:80 &"
}

# ─── Main ─────────────────────────────────────────────────────────────────────
main() {
  info "=== AI FinOps Engine — Cluster Setup ==="
  check_tools
  create_cluster
  install_prometheus
  verify_setup
}

main "$@"
