# AI FinOps Engine — Makefile
# Pattern: mirrors aiops-anomaly-detector conventions

.PHONY: up down demo test test-int inject status dashboard dry-run clean lint \
        port-forwards cluster-up cluster-down help

CLUSTER_NAME   := finops
NAMESPACE      := finops
PYTHON         := python3
PYTEST         := pytest
VENV           := .venv
PIP            := $(VENV)/bin/pip

# ─── Colors ───────────────────────────────────────────────────────────────────
GREEN  := \033[0;32m
YELLOW := \033[1;33m
NC     := \033[0m

# ─── Default ──────────────────────────────────────────────────────────────────
.DEFAULT_GOAL := help

help: ## Show available commands
	@echo ""
	@echo "AI FinOps Engine"
	@echo "================"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  $(GREEN)%-18s$(NC) %s\n", $$1, $$2}'
	@echo ""

# ─── Cluster ──────────────────────────────────────────────────────────────────
up: cluster-up port-forwards ## Start everything after reboot (cluster + port forwards)

cluster-up: ## Create k3d cluster finops + Prometheus
	@echo -e "$(GREEN)[make cluster-up]$(NC) Starting cluster '$(CLUSTER_NAME)'..."
	@bash scripts/cluster-up.sh

cluster-down: ## Stop k3d cluster (keep state)
	@echo -e "$(YELLOW)[make cluster-down]$(NC) Stopping cluster '$(CLUSTER_NAME)'..."
	k3d cluster stop $(CLUSTER_NAME)

clean: ## Delete k3d cluster completely
	@echo -e "$(YELLOW)[make clean]$(NC) Deleting cluster '$(CLUSTER_NAME)'..."
	k3d cluster delete $(CLUSTER_NAME) || true

down: cluster-down ## Alias for cluster-down

# ─── Port forwarding ──────────────────────────────────────────────────────────
port-forwards: ## Forward ports (Prometheus + Grafana)
	@bash scripts/port-forwards.sh

# ─── Development ──────────────────────────────────────────────────────────────
install: ## Create venv and install dependencies
	@echo -e "$(GREEN)[make install]$(NC) Setting up virtual environment..."
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	@echo -e "$(GREEN)✅$(NC) Dependencies installed"

# ─── Tests ────────────────────────────────────────────────────────────────────
test: ## Run unit tests (no API calls, no cluster required)
	@echo -e "$(GREEN)[make test]$(NC) Running unit tests..."
	$(VENV)/bin/pytest tests/ -m "not integration" $(ARGS)

test-int: ## Run integration tests (requires ANTHROPIC_API_KEY + k3d)
	@echo -e "$(YELLOW)[make test-int]$(NC) Running integration tests..."
	@test -n "$$ANTHROPIC_API_KEY" || (echo "❌ ANTHROPIC_API_KEY not set" && exit 1)
	$(VENV)/bin/pytest tests/ -m "integration" -v $(ARGS)

test-cov: ## Run tests with HTML coverage report
	$(VENV)/bin/pytest tests/ -m "not integration" \
		--cov=src --cov-report=html --cov-report=term-missing
	@echo -e "$(GREEN)✅$(NC) Coverage report: htmlcov/index.html"

# ─── Demo ─────────────────────────────────────────────────────────────────────
inject: ## Deploy wasteful workloads for demo
	@echo -e "$(GREEN)[make inject]$(NC) Injecting wasteful workloads..."
	kubectl config use-context k3d-$(CLUSTER_NAME)
	@bash scripts/inject-waste.sh

demo: ## Full demo flow: inject → detect → recommend → apply
	@echo -e "$(GREEN)[make demo]$(NC) Running full demo..."
	kubectl config use-context k3d-$(CLUSTER_NAME)
	@bash scripts/demo.sh

dry-run: ## Run detection without applying changes (DRY_RUN=true)
	@echo -e "$(YELLOW)[make dry-run]$(NC) Running in DRY_RUN mode..."
	DRY_RUN=true OPERATOR_MODE=SUGGEST \
		$(VENV)/bin/python -m src.main --dry-run

# ─── Status ───────────────────────────────────────────────────────────────────
status: ## Show CRD recommendations and current savings
	@echo ""
	@echo "=== FinOps Recommendations ==="
	kubectl config use-context k3d-$(CLUSTER_NAME)
	kubectl get finopsrecommendations -A 2>/dev/null \
		|| echo "(no CRDs yet — run 'make inject' first)"
	@echo ""
	@echo "=== Cluster Nodes ==="
	kubectl get nodes 2>/dev/null || echo "(cluster not running)"
	@echo ""
	@echo "=== Monitoring Pods ==="
	kubectl get pods -n monitoring 2>/dev/null | head -15 || true

dashboard: ## Open Grafana in browser
	@echo -e "$(GREEN)[make dashboard]$(NC) Opening Grafana..."
	xdg-open http://localhost:3001 2>/dev/null \
		|| echo "Open manually: http://localhost:3001  (admin / finops123)"

# ─── Linting ──────────────────────────────────────────────────────────────────
lint: ## Run flake8 + black check
	@echo -e "$(GREEN)[make lint]$(NC) Running linters..."
	$(VENV)/bin/flake8 src/ tests/ --max-line-length=100 --ignore=E501
	$(VENV)/bin/black --check src/ tests/

format: ## Format code with black
	$(VENV)/bin/black src/ tests/
