# AI Agent Guardrails — Safety & Control Framework

## Overview

This document outlines the guardrails (safety controls) implemented in the AI multi-agent DevOps platform and what additional controls should be added for production deployment. Guardrails ensure AI agents operate safely, predictably, and within defined boundaries.

---

## Why Guardrails Matter

AI agents can:
- Execute real commands against infrastructure (kubectl, AWS API)
- Make decisions autonomously without human review
- Loop infinitely if not controlled
- Leak sensitive information in responses
- Consume excessive resources (tokens, API calls)
- Take destructive actions (delete pods, terminate instances)

Guardrails prevent these failure modes while preserving the agent's usefulness.

---

## Guardrails Implemented (Current State)

### 1. Tool Isolation (Agent Sandboxing)

**What:** Each agent only has access to tools relevant to its role.

```
K8s Agent    → kubectl tools ONLY (cannot call AWS or cost tools)
AWS Agent    → EC2 + GitHub tools ONLY
Cost Agent   → CloudWatch + Cost Explorer ONLY
Deploy Agent → rollout/rollback tools ONLY
```

**Why:** Prevents unintended cross-domain actions. A cost optimization agent should never be able to restart a deployment.

**Implementation:** Tools are assigned per-agent in `crew.py`:
```python
k8s_agent = Agent(
    tools=[check_k8s_pod_health, check_k8s_node_health, ...],
    allow_delegation=False,  # Cannot delegate to other agents
)
```

---

### 2. Delegation Control

**What:** Sub-agents cannot delegate work to each other. Only the Manager Agent can delegate.

```python
allow_delegation=False  # Set on ALL specialist agents
```

**Why:** Prevents circular delegation loops (Agent A → Agent B → Agent A → ...) and ensures the Manager maintains control of the workflow.

---

### 3. Read-Only Default Posture

**What:** Most tools are read-only (observe, don't modify).

| Tool Category | Action Type | Risk Level |
|---|---|---|
| `check_k8s_pod_health` | READ | Low |
| `check_aws_ec2_health` | READ | Low |
| `get_cost_summary` | READ | Low |
| `get_pod_crash_logs` | READ | Low |
| `rollback_deployment` | WRITE | High |
| `restart_deployment` | WRITE | Medium |

**Why:** The agent should primarily inform and recommend. Destructive actions require additional justification.

---

### 4. Execution Timeout (LangGraph version)

**What:** Sub-agents have a maximum of 5 tool-call iterations.

```python
for _ in range(5):  # Max 5 tool calls per sub-agent
    response = llm.invoke(messages)
    if not response.tool_calls:
        break
```

**Why:** Prevents infinite loops where the agent keeps calling tools without converging on an answer.

---

### 5. Rate Limiting (Natural Throttle)

**What:** Groq free tier enforces 12,000 tokens per minute (TPM).

**Why:** Acts as a natural circuit breaker — if the agent enters a runaway loop, it hits the rate limit and fails gracefully instead of consuming unlimited resources.

**Production equivalent:** Set explicit token budgets per crew run.

---

### 6. Secrets Management

**What:** 
- Credentials stored in `.env` (never committed to Git)
- `.gitignore` excludes `.env`, `venv/`, `__pycache__/`
- `.env.example` provided as template without real values

**Why:** Prevents credential exposure in source control.

---

### 7. Error Handling (Graceful Degradation)

**What:** Tools return error messages instead of crashing:

```python
def check_k8s_pod_health(namespace):
    output = _run_kubectl(f"get pods -n {namespace}")
    if "error" in output.lower():
        return f"K8s pod check failed: {output}"  # Graceful error
```

**Why:** If kubectl is unreachable, the agent reports the issue instead of crashing the entire crew run.

---

### 8. Subprocess Timeout

**What:** All kubectl commands have a 30-second timeout:

```python
result = subprocess.run(
    f"kubectl {cmd}",
    shell=True,
    capture_output=True,
    timeout=30,  # Kill after 30s
)
```

**Why:** Prevents hanging processes if the cluster is unreachable.

---

### 9. Output Truncation

**What:** Tool outputs are capped to prevent context overflow:

```python
return f"Crash logs for '{pod_name}':\n{output[-2000:]}"  # Last 2000 chars only
```

**Why:** LLMs have context limits. Dumping 50KB of logs would break the agent.

---

### 10. Tracing & Audit Trail

**What:** Every crew execution is traced on app.crewai.com:
- Which agent handled which task
- Tool calls with arguments
- LLM reasoning at each step
- Timing and token usage

**Why:** Full auditability — you can review what the agent did and why.

---

## Guardrails to Add for Production

### 11. Human Approval Gate for Destructive Actions

```python
# Before rollback/restart, require human confirmation
DANGEROUS_TOOLS = ["rollback_deployment", "restart_deployment"]

class ApprovalRequiredTool(BaseTool):
    def _run(self, **kwargs):
        # Send Slack/Teams notification
        # Wait for approval (webhook callback)
        # Only proceed if approved within timeout
        if not get_approval(tool_name, kwargs, timeout=300):
            return "Action blocked: no human approval received within 5 minutes"
        return actual_tool_execution(**kwargs)
```

**Status:** Not implemented (POC runs in safe mode)

---

### 12. Namespace/Cluster Scoping

```python
ALLOWED_NAMESPACES = ["default", "order-service", "payment-service", "user-service"]
BLOCKED_NAMESPACES = ["kube-system", "argocd", "monitoring", "production"]

def check_k8s_pod_health(namespace):
    if namespace in BLOCKED_NAMESPACES:
        return f"Access denied: namespace '{namespace}' is restricted"
```

**Status:** Not implemented (agent currently has access to all namespaces)

---

### 13. Token Budget Per Run

```python
MAX_TOKENS_PER_RUN = 50000  # Kill run if exceeded

crew = Crew(
    max_tokens=MAX_TOKENS_PER_RUN,
    # OR implement via LiteLLM callback
)
```

**Status:** Not implemented (relies on Groq rate limit as natural cap)

---

### 14. Output Validation (Hallucination Check)

```python
def validate_agent_output(output: str) -> str:
    # Check for fabricated kubectl commands
    if "kubectl delete" in output and "recommended" in output:
        return output.replace("kubectl delete", "[BLOCKED: destructive command]")
    
    # Check for made-up resource names
    if "pod/" in output and not verify_resource_exists(output):
        return output + "\n⚠️ WARNING: Some resources mentioned may not exist"
    
    return output
```

**Status:** Not implemented (agents sometimes hallucinate resource names)

---

### 15. Credential Rotation & Vault Integration

```python
# Production: fetch secrets from HashiCorp Vault
import hvac

def get_aws_credentials():
    client = hvac.Client(url=VAULT_URL)
    secret = client.secrets.kv.read_secret_version(path="aws/devops-agent")
    return secret["data"]["data"]
```

**Status:** Not implemented (uses static .env credentials)

---

### 16. Network Segmentation

```
Production setup:
- Agent runs in a dedicated namespace with NetworkPolicy
- Can only reach: K8s API server, AWS APIs, GitHub API
- Cannot reach: databases, internal services, other clusters
```

**Status:** Not implemented (agent runs locally with full network access)

---

### 17. Alerting on Agent Failures

```python
# Send PagerDuty/Slack alert if agent fails
def on_crew_failure(error):
    slack_webhook.send(
        text=f"🚨 DevOps AI Agent FAILED\nError: {error}\nTime: {datetime.now()}"
    )
```

**Status:** Not implemented (errors only shown in terminal/traces)

---

### 18. Input Sanitization

```python
def sanitize_user_input(query: str) -> str:
    # Prevent prompt injection
    dangerous_patterns = [
        "ignore previous instructions",
        "you are now",
        "forget your role",
        "execute this command:",
    ]
    for pattern in dangerous_patterns:
        if pattern.lower() in query.lower():
            return "Invalid query: potential prompt injection detected"
    return query
```

**Status:** Not implemented (agent accepts any input)

---

### 19. Cost Ceiling

```python
# Alert if agent finds waste > threshold but DO NOT auto-terminate
COST_ALERT_THRESHOLD = 100  # dollars

def find_idle_instances():
    ...
    if estimated_waste > COST_ALERT_THRESHOLD:
        alert_finops_team(waste=estimated_waste)
        return f"ALERT: ${estimated_waste}/month waste detected. Sent to FinOps for review."
    # NEVER auto-terminate instances
```

**Status:** Partially implemented (reports waste, doesn't auto-terminate)

---

### 20. Rollback on Agent-Initiated Changes

```python
# If agent makes a change and health check fails within 5 min → auto-rollback
def safe_restart(deployment, namespace):
    # Take snapshot of current state
    pre_state = get_deployment_state(deployment, namespace)
    
    # Execute restart
    restart_deployment(deployment, namespace)
    
    # Wait and verify
    time.sleep(60)
    if not verify_healthy(deployment, namespace):
        rollback_deployment(deployment, namespace)
        return "Restart failed health check — auto-rolled back"
    
    return "Restart successful — health check passed"
```

**Status:** Not implemented (restart/rollback are separate manual actions)

---

## Guardrail Maturity Model

| Level | Description | Current State |
|---|---|---|
| **L1: Observe** | Agent can only read and report | ✅ Implemented |
| **L2: Recommend** | Agent suggests actions, human executes | ✅ Implemented |
| **L3: Act with Approval** | Agent executes after human approves | ⚠️ Partial (tools exist but no approval gate) |
| **L4: Act Autonomously** | Agent executes without human intervention | ❌ Not safe without L3 guardrails |
| **L5: Self-Healing** | Agent detects + fixes + validates autonomously | ❌ Future goal |

**Current platform is at Level 2** — it observes infrastructure and recommends actions. Moving to L3+ requires implementing the approval gate (guardrail #11).

---

## Summary Table

| # | Guardrail | Status | Risk Mitigated |
|---|---|---|---|
| 1 | Tool Isolation | ✅ Done | Cross-domain accidents |
| 2 | Delegation Control | ✅ Done | Circular loops |
| 3 | Read-Only Default | ✅ Done | Unintended modifications |
| 4 | Execution Timeout | ✅ Done | Infinite loops |
| 5 | Rate Limiting | ✅ Done | Runaway token consumption |
| 6 | Secrets Management | ✅ Done | Credential exposure |
| 7 | Error Handling | ✅ Done | Crash cascades |
| 8 | Subprocess Timeout | ✅ Done | Hanging processes |
| 9 | Output Truncation | ✅ Done | Context overflow |
| 10 | Tracing/Audit | ✅ Done | Accountability |
| 11 | Human Approval Gate | ❌ TODO | Unauthorized changes |
| 12 | Namespace Scoping | ❌ TODO | Access to production |
| 13 | Token Budget | ❌ TODO | Cost control |
| 14 | Output Validation | ❌ TODO | Hallucinations |
| 15 | Vault Integration | ❌ TODO | Static credentials |
| 16 | Network Segmentation | ❌ TODO | Lateral movement |
| 17 | Failure Alerting | ❌ TODO | Silent failures |
| 18 | Input Sanitization | ❌ TODO | Prompt injection |
| 19 | Cost Ceiling | ⚠️ Partial | Auto-termination |
| 20 | Rollback on Failure | ❌ TODO | Failed changes |

---

## Interview Talking Points

When asked "how do you ensure AI agents are safe?":

> "I follow a layered guardrail approach. First, tool isolation — each agent only has access to tools for its domain. Second, read-only by default — agents observe and recommend, they don't auto-execute destructive actions. Third, execution limits — timeouts and max iterations prevent runaway loops. Fourth, full tracing — every decision is auditable. For production, I'd add human approval gates for destructive actions, namespace scoping to restrict cluster access, and input sanitization to prevent prompt injection."

This shows you think about safety systematically, not as an afterthought.
