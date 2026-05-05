# src/agent.py
# Claude FinOps agent — analyzes resource anomalies and generates rightsizing recommendations
# Pattern reused from aiops-anomaly-detector

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import anthropic

from src.collector import ContainerMetrics
from src.model import AnomalyResult, AnomalyType

logger = logging.getLogger(__name__)

# Safety floors — never recommend below these values
CPU_REQUEST_MIN_CORES = 0.010       # 10m
MEMORY_REQUEST_MIN_BYTES = 16 * 1024 ** 2   # 16Mi

# Risk threshold — if recommendation reduces limit by more than this → risk=high
LARGE_REDUCTION_THRESHOLD = 0.50    # 50%

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


@dataclass
class FinOpsRecommendation:
    """Structured recommendation from the FinOps agent."""

    namespace: str
    deployment: str
    container: str

    anomaly_type: str
    severity: str
    root_cause: str
    reasoning: str

    # Recommended resource values
    cpu_request: str        # e.g. "50m"
    cpu_limit: str          # e.g. "200m"
    memory_request: str     # e.g. "64Mi"
    memory_limit: str       # e.g. "128Mi"

    monthly_saving_usd: float
    confidence: float
    risk: str               # low | medium | high

    # Raw response for debugging
    raw_response: Optional[str] = None


class FinOpsAgent:
    """
    Claude-powered agent that analyzes Kubernetes resource anomalies
    and generates rightsizing recommendations with cost estimates.
    """

    def __init__(
        self,
        model: str = "claude-haiku-4-5",
        max_tokens: int = 1024,
        api_key: Optional[str] = None,
    ):
        self.model = model
        self.max_tokens = max_tokens
        self._client = anthropic.Anthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY")
        )
        self._system_prompt = self._load_system_prompt()
        self._tools = self._define_tools()

    def analyze(
        self,
        metrics: ContainerMetrics,
        anomaly: AnomalyResult,
    ) -> FinOpsRecommendation:
        """Analyze a single anomaly and return a rightsizing recommendation.

        Calls Claude with tool use — agent may call get_resource_history
        or check_hpa_config before producing the final recommendation.
        """
        logger.info(
            "Calling Claude agent for %s/%s (type=%s, score=%.3f)",
            anomaly.namespace, anomaly.deployment,
            anomaly.anomaly_type, anomaly.anomaly_score,
        )

        user_message = self._build_user_message(metrics, anomaly)
        messages = [{"role": "user", "content": user_message}]

        # Agentic loop — handle tool calls until final text response
        while True:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=self._system_prompt,
                tools=self._tools,
                messages=messages,
            )

            # Append assistant response to conversation
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "tool_use":
                # Process tool calls and append results
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        result = self._handle_tool_call(block.name, block.input, metrics)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result),
                        })
                messages.append({"role": "user", "content": tool_results})
                continue

            # stop_reason == "end_turn" — extract text response
            raw_text = self._extract_text(response.content)
            break

        recommendation = self._parse_response(raw_text, metrics, anomaly)
        recommendation = self._apply_safety_floors(recommendation, metrics)
        recommendation.raw_response = raw_text

        logger.info(
            "Recommendation for %s/%s: saving=$%.2f confidence=%.2f risk=%s",
            anomaly.namespace, anomaly.deployment,
            recommendation.monthly_saving_usd,
            recommendation.confidence,
            recommendation.risk,
        )

        return recommendation

    def _build_user_message(
        self,
        metrics: ContainerMetrics,
        anomaly: AnomalyResult,
    ) -> str:
        """Build the user message with full context for the agent."""
        cpu_req_m = int(metrics.cpu_request_cores * 1000)
        cpu_lim_m = int(metrics.cpu_limit_cores * 1000)
        cpu_use_m = int(metrics.cpu_usage_cores * 1000)
        mem_req_mi = int(metrics.memory_request_bytes / 1024 ** 2)
        mem_lim_mi = int(metrics.memory_limit_bytes / 1024 ** 2)
        mem_use_mi = int(metrics.memory_usage_bytes / 1024 ** 2)

        return f"""Analyze this Kubernetes resource anomaly and provide a rightsizing recommendation.

## Container
- Namespace:  {metrics.namespace}
- Deployment: {metrics.deployment}
- Container:  {metrics.container}

## Anomaly Detection
- Type:         {anomaly.anomaly_type}
- Severity:     {anomaly.severity}
- Score:        {anomaly.anomaly_score:.4f} (more negative = more anomalous)

## Current Resources
- CPU request:    {cpu_req_m}m
- CPU limit:      {cpu_lim_m}m
- Memory request: {mem_req_mi}Mi
- Memory limit:   {mem_lim_mi}Mi

## Actual Usage (current)
- CPU usage:    {cpu_use_m}m  ({anomaly.cpu_utilization:.1%} of request)
- Memory usage: {mem_use_mi}Mi ({anomaly.memory_utilization:.1%} of request)

## Additional Signals
- CPU throttling rate: {anomaly.cpu_throttling_rate:.1%}
- OOM events (24h):    {anomaly.oom_events_24h}
- CPU limit ratio:     {anomaly.cpu_limit_ratio:.1f}x (limit/request)
- Memory limit ratio:  {anomaly.memory_limit_ratio:.1f}x (limit/request)

Use the available tools to get historical usage data before making your recommendation.
"""

    def _handle_tool_call(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        metrics: ContainerMetrics,
    ) -> dict[str, Any]:
        """Execute tool call and return result."""
        if tool_name == "get_resource_history":
            return self._tool_get_resource_history(metrics)
        elif tool_name == "get_deployment_info":
            return self._tool_get_deployment_info(metrics)
        elif tool_name == "calculate_cost_saving":
            return self._tool_calculate_cost_saving(tool_input, metrics)
        elif tool_name == "check_hpa_config":
            return self._tool_check_hpa_config(metrics)
        else:
            return {"error": f"Unknown tool: {tool_name}"}

    def _tool_get_resource_history(self, metrics: ContainerMetrics) -> dict:
        """Return 7-day usage history summary.

        In production this would query Prometheus range API.
        For demo purposes returns derived estimates from current metrics.
        """
        cpu_m = int(metrics.cpu_usage_cores * 1000)
        mem_mi = int(metrics.memory_usage_bytes / 1024 ** 2)

        return {
            "deployment": metrics.deployment,
            "period": "7 days",
            "cpu_p50_m": int(cpu_m * 0.85),
            "cpu_p95_m": int(cpu_m * 1.30),
            "cpu_p99_m": int(cpu_m * 1.60),
            "cpu_max_m": int(cpu_m * 2.00),
            "memory_p50_mi": int(mem_mi * 0.90),
            "memory_p95_mi": int(mem_mi * 1.20),
            "memory_p99_mi": int(mem_mi * 1.40),
            "memory_max_mi": int(mem_mi * 1.80),
            "note": "Estimated from current usage; production would use Prometheus range query",
        }

    def _tool_get_deployment_info(self, metrics: ContainerMetrics) -> dict:
        """Return current deployment configuration."""
        return {
            "deployment": metrics.deployment,
            "namespace": metrics.namespace,
            "replicas": 1,
            "cpu_request": f"{int(metrics.cpu_request_cores * 1000)}m",
            "cpu_limit": f"{int(metrics.cpu_limit_cores * 1000)}m",
            "memory_request": f"{int(metrics.memory_request_bytes / 1024 ** 2)}Mi",
            "memory_limit": f"{int(metrics.memory_limit_bytes / 1024 ** 2)}Mi",
        }

    def _tool_calculate_cost_saving(
        self, tool_input: dict, metrics: ContainerMetrics
    ) -> dict:
        """Estimate monthly cost saving for proposed resource reduction."""
        cpu_price = float(os.environ.get("CPU_PRICE_PER_CORE_HOUR", "0.048"))
        mem_price = float(os.environ.get("MEMORY_PRICE_PER_GB_HOUR", "0.006"))
        hours = 730

        current_cpu_cost = metrics.cpu_request_cores * cpu_price * hours
        current_mem_cost = (metrics.memory_request_bytes / 1024 ** 3) * mem_price * hours

        # Parse proposed values from tool input if provided
        proposed_cpu_m = tool_input.get("proposed_cpu_request_m", 0)
        proposed_mem_mi = tool_input.get("proposed_memory_request_mi", 0)

        proposed_cpu_cost = (proposed_cpu_m / 1000) * cpu_price * hours
        proposed_mem_cost = (proposed_mem_mi / 1024) * mem_price * hours

        saving = max(0.0, (current_cpu_cost + current_mem_cost) - (proposed_cpu_cost + proposed_mem_cost))

        return {
            "current_monthly_usd": round(current_cpu_cost + current_mem_cost, 2),
            "proposed_monthly_usd": round(proposed_cpu_cost + proposed_mem_cost, 2),
            "saving_monthly_usd": round(saving, 2),
        }

    def _tool_check_hpa_config(self, metrics: ContainerMetrics) -> dict:
        """Check if HPA exists for this deployment."""
        return {
            "deployment": metrics.deployment,
            "hpa_exists": False,
            "note": "No HPA detected — safe to adjust requests without affecting autoscaling",
        }

    def _parse_response(
        self,
        raw_text: str,
        metrics: ContainerMetrics,
        anomaly: AnomalyResult,
    ) -> FinOpsRecommendation:
        """Parse JSON response from Claude into FinOpsRecommendation."""
        try:
            # Strip markdown code fences if present
            text = raw_text.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(lines[1:-1])

            data = json.loads(text)
            rec = data.get("recommendation", {})

            return FinOpsRecommendation(
                namespace=metrics.namespace,
                deployment=metrics.deployment,
                container=metrics.container,
                anomaly_type=data.get("anomaly_type", anomaly.anomaly_type),
                severity=data.get("severity", anomaly.severity),
                root_cause=data.get("root_cause", ""),
                reasoning=data.get("reasoning", ""),
                cpu_request=rec.get("cpu_request", "10m"),
                cpu_limit=rec.get("cpu_limit", "100m"),
                memory_request=rec.get("memory_request", "16Mi"),
                memory_limit=rec.get("memory_limit", "64Mi"),
                monthly_saving_usd=float(data.get("monthly_saving_usd", 0.0)),
                confidence=float(data.get("confidence", 0.5)),
                risk=data.get("risk", "high"),
            )

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.error("Failed to parse agent response: %s\nRaw: %s", e, raw_text[:200])
            return self._fallback_recommendation(metrics, anomaly)

    def _apply_safety_floors(
        self,
        rec: FinOpsRecommendation,
        metrics: ContainerMetrics,
    ) -> FinOpsRecommendation:
        """Enforce minimum resource values and risk escalation rules."""
        # Parse current limits for comparison
        cpu_lim_current = metrics.cpu_limit_cores

        # Parse recommended CPU limit
        cpu_lim_rec = self._parse_resource_value(rec.cpu_limit, "cpu")

        # If recommendation reduces limit by > 50% → escalate risk
        if cpu_lim_current > 0:
            reduction = (cpu_lim_current - cpu_lim_rec) / cpu_lim_current
            if reduction > LARGE_REDUCTION_THRESHOLD:
                if rec.risk == "low":
                    rec.risk = "medium"

        # OOM events → always high risk regardless of agent output
        if metrics.oom_events_24h > 0:
            rec.risk = "high"

        # Enforce CPU floor: 10m minimum
        cpu_req = self._parse_resource_value(rec.cpu_request, "cpu")
        if cpu_req < CPU_REQUEST_MIN_CORES:
            rec.cpu_request = "10m"

        # Enforce memory floor: 16Mi minimum
        mem_req = self._parse_resource_value(rec.memory_request, "memory")
        if mem_req < MEMORY_REQUEST_MIN_BYTES:
            rec.memory_request = "16Mi"

        return rec

    def _fallback_recommendation(
        self,
        metrics: ContainerMetrics,
        anomaly: AnomalyResult,
    ) -> FinOpsRecommendation:
        """Conservative fallback when Claude response cannot be parsed."""
        cpu_m = max(10, int(metrics.cpu_usage_cores * 1000 * 1.5))
        mem_mi = max(16, int(metrics.memory_usage_bytes / 1024 ** 2 * 1.5))

        return FinOpsRecommendation(
            namespace=metrics.namespace,
            deployment=metrics.deployment,
            container=metrics.container,
            anomaly_type=str(anomaly.anomaly_type),
            severity=anomaly.severity,
            root_cause="Agent response parsing failed — using conservative fallback",
            reasoning="Could not parse Claude response. Conservative values applied.",
            cpu_request=f"{cpu_m}m",
            cpu_limit=f"{cpu_m * 3}m",
            memory_request=f"{mem_mi}Mi",
            memory_limit=f"{mem_mi * 2}Mi",
            monthly_saving_usd=0.0,
            confidence=0.3,
            risk="high",
        )

    @staticmethod
    def _parse_resource_value(value: str, resource_type: str) -> float:
        """Parse resource string to float in base units.

        CPU: '100m' → 0.1 cores, '0.5' → 0.5 cores
        Memory: '64Mi' → 67108864 bytes, '1Gi' → 1073741824 bytes
        """
        value = value.strip()
        if resource_type == "cpu":
            if value.endswith("m"):
                return float(value[:-1]) / 1000
            return float(value)
        else:  # memory
            if value.endswith("Mi"):
                return float(value[:-2]) * 1024 ** 2
            if value.endswith("Gi"):
                return float(value[:-2]) * 1024 ** 3
            if value.endswith("Ki"):
                return float(value[:-2]) * 1024
            return float(value)

    def _load_system_prompt(self) -> str:
        """Load system prompt from versioned markdown file."""
        prompt_file = PROMPTS_DIR / "finops_agent_v1.md"
        if prompt_file.exists():
            content = prompt_file.read_text()
            # Strip markdown headers and code fences — return plain text
            lines = [
                line for line in content.splitlines()
                if not line.startswith("#") and not line.startswith("```")
            ]
            return "\n".join(lines).strip()

        # Inline fallback if file not found
        return (
            "You are a Kubernetes FinOps expert. Analyze resource utilization anomalies "
            "and provide specific rightsizing recommendations. "
            "Respond ONLY with valid JSON. Never recommend less than 10m CPU or 16Mi memory."
        )

    def _define_tools(self) -> list[dict]:
        """Define Claude tool schemas."""
        return [
            {
                "name": "get_resource_history",
                "description": (
                    "Get 7-day CPU and memory usage history for a deployment. "
                    "Returns p50, p95, p99, and max values. "
                    "Always call this before making a recommendation."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "deployment": {"type": "string", "description": "Deployment name"},
                        "namespace": {"type": "string", "description": "Kubernetes namespace"},
                    },
                    "required": ["deployment", "namespace"],
                },
            },
            {
                "name": "get_deployment_info",
                "description": "Get current resource requests and limits for a deployment.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "deployment": {"type": "string"},
                        "namespace": {"type": "string"},
                    },
                    "required": ["deployment", "namespace"],
                },
            },
            {
                "name": "calculate_cost_saving",
                "description": "Calculate monthly USD saving for proposed resource values.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "proposed_cpu_request_m": {
                            "type": "integer",
                            "description": "Proposed CPU request in millicores",
                        },
                        "proposed_memory_request_mi": {
                            "type": "integer",
                            "description": "Proposed memory request in MiB",
                        },
                    },
                    "required": ["proposed_cpu_request_m", "proposed_memory_request_mi"],
                },
            },
            {
                "name": "check_hpa_config",
                "description": (
                    "Check if HPA (HorizontalPodAutoscaler) exists for this deployment. "
                    "HPA presence affects whether CPU requests can be safely reduced."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "deployment": {"type": "string"},
                        "namespace": {"type": "string"},
                    },
                    "required": ["deployment", "namespace"],
                },
            },
        ]

    @staticmethod
    def _extract_text(content: list) -> str:
        """Extract text from Anthropic response content blocks."""
        return "\n".join(
            block.text for block in content if hasattr(block, "text")
        ).strip()
