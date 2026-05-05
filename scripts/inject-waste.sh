#!/usr/bin/env bash
# scripts/inject-waste.sh
# Создаёт 3 wasteful deployments для демо AI FinOps Engine

set -euo pipefail

GREEN='\033[0;32m'
NC='\033[0m'
info() { echo -e "${GREEN}[inject-waste]${NC} $*"; }

NAMESPACE="${NAMESPACE:-default}"

info "Creating wasteful workloads in namespace '${NAMESPACE}'..."

# ─── waste-demo-1: Сильный over-provisioning CPU ──────────────────────────────
# CPU limit 2000m, реальное использование nginx ~5-20m
kubectl create deployment waste-demo-1 \
  --image=nginx:alpine \
  --replicas=3 \
  --namespace="${NAMESPACE}" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl set resources deployment waste-demo-1 \
  --namespace="${NAMESPACE}" \
  --requests=cpu=200m,memory=256Mi \
  --limits=cpu=2000m,memory=1Gi

info "waste-demo-1 created: cpu_limit=2000m (actual usage ~15m) — OVER_PROVISIONED"

# ─── waste-demo-2: Over-provisioning Memory ───────────────────────────────────
# Memory request 512Mi, реальное использование python minimal ~30Mi
kubectl create deployment waste-demo-2 \
  --image=python:3.12-alpine \
  --replicas=2 \
  --namespace="${NAMESPACE}" \
  --dry-run=client -o yaml | kubectl apply -f -

# Добавляем command чтобы pod не крашился
kubectl patch deployment waste-demo-2 \
  --namespace="${NAMESPACE}" \
  --type='json' \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/command","value":["python","-c","import time; time.sleep(86400)"]}]'

kubectl set resources deployment waste-demo-2 \
  --namespace="${NAMESPACE}" \
  --requests=cpu=500m,memory=512Mi \
  --limits=cpu=1000m,memory=2Gi

info "waste-demo-2 created: memory_request=512Mi (actual usage ~30Mi) — OVER_PROVISIONED"

# ─── waste-demo-3: Under-provisioning — будет throttling ─────────────────────
# CPU limit 20m, но nginx под нагрузкой потребляет больше → throttling
kubectl create deployment waste-demo-3 \
  --image=nginx:alpine \
  --replicas=1 \
  --namespace="${NAMESPACE}" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl set resources deployment waste-demo-3 \
  --namespace="${NAMESPACE}" \
  --requests=cpu=10m,memory=16Mi \
  --limits=cpu=20m,memory=32Mi

info "waste-demo-3 created: cpu_limit=20m (too low for nginx) — UNDER_PROVISIONED"

# ─── Ждём pods ────────────────────────────────────────────────────────────────
info "Waiting for pods to be Running..."
kubectl wait deployment/waste-demo-1 \
  --namespace="${NAMESPACE}" \
  --for=condition=Available \
  --timeout=60s || true
kubectl wait deployment/waste-demo-2 \
  --namespace="${NAMESPACE}" \
  --for=condition=Available \
  --timeout=60s || true
kubectl wait deployment/waste-demo-3 \
  --namespace="${NAMESPACE}" \
  --for=condition=Available \
  --timeout=60s || true

echo ""
kubectl get deployments -n "${NAMESPACE}" -l 'app in (waste-demo-1,waste-demo-2,waste-demo-3)'
echo ""
info "✅ Wasteful workloads injected!"
info "Wait 5 minutes for Prometheus to collect metrics, then run 'make demo'"
