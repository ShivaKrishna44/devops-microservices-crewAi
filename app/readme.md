# DevOps AI Agent — CrewAI Multi-Agent Platform

## Overview
This project uses **CrewAI** with a **hierarchical process** (manager agent auto-delegates) to run a team of specialized DevOps agents that monitor infrastructure, detect issues, and optimize costs.

---

## Architecture (CrewAI)

```text
                    User Query
                        │
                        ▼
              ┌─────────────────┐
              │  MANAGER AGENT  │  (auto — CrewAI hierarchical)
              │  (decides who   │
              │   to delegate)  │
              └─────────────────┘
                        │
      ┌─────────┬──────┼──────┬──────────┬───────────┐
      ▼         ▼      ▼      ▼          ▼           ▼
 ┌────────┐ ┌──────┐ ┌──────┐ ┌────────┐ ┌────────┐ ┌──────────┐
 │  K8s   │ │ AWS  │ │Deploy│ │  Cost  │ │Incident│ │Migration │
 │ Agent  │ │Agent │ │Agent │ │ Agent  │ │ Agent  │ │  Agent   │
 └────────┘ └──────┘ └──────┘ └────────┘ └────────┘ └──────────┘
      │         │       │          │          │           │
  K8s tools  AWS+GH  rollout   cost scan  crash logs  Jenkinsfile
              tools    tools     tools     + events    converter
```

---

## vs Previous LangGraph Architecture

| Aspect | LangGraph (before) | CrewAI (now) |
|--------|-------------------|--------------|
| Orchestration | Manual StateGraph + supervisor node | Built-in hierarchical process |
| Code complexity | 200+ lines in multi_agent.py | ~50 lines in crew.py |
| Agent routing | Custom conditional edges | Manager auto-delegates |
| Tool loops | Manual 5-iteration loop | CrewAI handles automatically |
| Memory | Manual state dict | Built-in (optional) |
| Delegation | Not supported | Native agent-to-agent delegation |

---

## Setup Guide (Step-by-Step)

### Prerequisites
- **Python 3.10 to 3.13** (CrewAI does NOT support Python 3.14)
- **Git Bash** or **CMD** on Windows
- **AWS CLI** configured (for EKS/EC2 checks)
- **kubectl** configured (for K8s checks)
- **Groq API key** (free at https://console.groq.com)
- **GitHub PAT** (for workflow status checks)

### Step 1: Check Python Version

```bash
py -0          # Lists all installed Python versions
```

You need Python 3.10–3.13. If you only have 3.14, install 3.12 or 3.13 from python.org.

### Step 2: Create Virtual Environment (MUST be Python ≤3.13)

```bash
cd C:\Devops\Repository\devops-microservices-crewAi\app

# Delete old venv if exists
rm -rf venv                          # Git Bash
# OR: rmdir /s /q venv               # CMD

# Create with Python 3.13 (adjust path if needed)
py -3.13 -m venv venv
```

### Step 3: Activate venv

**Important**: In Git Bash, `activate.bat` does NOT work. Use:

```bash
# Git Bash — use source:
source venv/Scripts/activate

# CMD — use .bat:
venv\Scripts\activate.bat
```

**Verify** it's the right Python:
```bash
python --version   # Must say 3.13.x, NOT 3.14
```

If `python --version` still shows 3.14 (venv activation not working in Git Bash),
use the venv python directly for ALL commands:

```bash
venv/Scripts/python.exe --version    # This ALWAYS uses the venv
```

### Step 4: Install Dependencies

```bash
# If activation worked:
pip install --upgrade pip
pip install -r requirements.txt

# If activation didn't work (Git Bash issues), use direct path:
venv/Scripts/python.exe -m pip install --upgrade pip
venv/Scripts/python.exe -m pip install -r requirements.txt
```

### Step 5: Configure Environment Variables

```bash
cp .env.example .env
```

Edit `.env` with your actual keys:

```env
GROQ_API_KEY=gsk_your_key_here
CREWAI_LLM=groq/llama-3.1-8b-instant
GITHUB_TOKEN=ghp_your_token_here
AWS_ACCESS_KEY_ID=AKIA_your_key
AWS_SECRET_ACCESS_KEY=your_secret
AWS_DEFAULT_REGION=us-east-1
TARGET_REPO=YourOrg/your-repo-name

# Tracing (get API key from app.crewai.com → Settings → Personal Access Token)
CREWAI_TRACING_ENABLED=true
CREWAI_API_KEY=your-crewai-personal-access-token
```

### Step 6: Connect to EKS Cluster (for K8s checks)

```bash
aws eks update-kubeconfig --name expense-dev --region us-east-1
kubectl get nodes   # Verify connection
```

### Step 7: Run the Agent

```bash
# Full health check:
venv/Scripts/python.exe agent.py

# Custom query:
venv/Scripts/python.exe agent.py "check kubernetes pods"
venv/Scripts/python.exe agent.py "find cost waste"
venv/Scripts/python.exe agent.py "check GitHub Actions status"
```

### Step 8: Run Web Dashboard

```bash
venv/Scripts/python.exe -m uvicorn web.app:app --reload --port 8000
```

Open: http://localhost:8000

### Step 9: View Traces

Go to: https://app.crewai.com → **Traces** (left sidebar)

---

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| `No matching distribution found for crewai` | Python 3.14 (unsupported) | Use Python 3.13 venv |
| `python --version` shows 3.14 after activate | Git Bash activation issue | Use `venv/Scripts/python.exe` directly |
| `cache_breakpoint is unsupported` | Groq doesn't support CrewAI cache params | `litellm_patch.py` handles this (already included) |
| `enable_cache is unsupported` | Same issue, different param | Remove `enable_cache` from LLM config |
| `'required' present but 'properties' is missing` | Groq rejects tools with no parameters | Add a dummy param with default value to all no-arg tools |
| `Rate limit reached` (TPM 12000) | Groq free tier limit | Wait 3s between runs, or use `groq/llama-3.1-8b-instant` (smaller) |
| `kubectl error: no such host` | EKS cluster unreachable | Run `aws eks update-kubeconfig --name <cluster> --region <region>` |
| `conflicting dependencies` (httpx) | `langchain-groq` conflicts with crewai | Remove `langchain-groq` from requirements (not needed) |
| `UnicodeEncodeError` in CLI | Windows cp1252 encoding | Set `PYTHONUTF8=1` or use CMD instead of Git Bash |
| Traces empty on app.crewai.com | No API key configured | Add `CREWAI_API_KEY` from Personal Access Token in settings |

---

## Key Lessons Learned

1. **CrewAI requires Python <3.14** — always create a venv with 3.13 or 3.12
2. **Git Bash venv activation is unreliable** — use `venv/Scripts/python.exe` directly
3. **Groq + CrewAI needs a monkey patch** — `litellm_patch.py` strips unsupported params
4. **All tools MUST have at least one parameter** — Groq rejects empty tool schemas
5. **Use `@tool("Tool Name")` from `crewai.tools`** — not `langchain_core.tools`
6. **`crewai[litellm]` extra is required** for non-native LLM providers like Groq
7. **Free Groq tier = 12K TPM** — use smaller models or add delays for multi-agent runs

---

## Quick Start (TL;DR)

```bash
cd C:\Devops\Repository\devops-microservices-crewAi\app
py -3.13 -m venv venv
venv/Scripts/python.exe -m pip install -r requirements.txt
# Edit .env with your keys
venv/Scripts/python.exe agent.py "check kubernetes pods"
# Web UI:
venv/Scripts/python.exe -m uvicorn web.app:app --reload --port 8000
```

---

## File Structure

```
app/
├── agent.py              ← Main entry point (CrewAI)
├── crew.py               ← CrewAI agents, tasks, and crew definition
├── multi_agent_run.py    ← Alternative entry with custom query support
├── config.py             ← Settings + environment configuration
├── requirements.txt      ← Python dependencies (includes crewai)
├── .env.example          ← Template for environment variables
├── tools/                ← All agent tools (reused from LangChain @tool)
│   ├── aws_tools.py      ← EC2 health check
│   ├── github_tools.py   ← GitHub Actions status
│   ├── k8s_tools.py      ← Pod/node/deployment health
│   ├── deploy_tools.py   ← Rollout status, rollback, restart
│   ├── incident_tools.py ← Crash logs, events, diagnosis
│   ├── cost_tools.py     ← Idle instances, EBS waste, spend
│   └── migration_tools.py← Jenkinsfile → GHA conversion
├── web/
│   └── app.py            ← FastAPI web dashboard
└── graph/                ← (DEPRECATED) Old LangGraph implementation
    ├── workflow.py        ← Single-agent LangGraph (still works)
    └── multi_agent.py    ← Old supervisor pattern (replaced by crew.py)
```

---

## CrewAI Agents

| Agent | Role | Tools |
|-------|------|-------|
| **K8s Agent** | Kubernetes Specialist | pod health, node health, deployments, crash logs, events, diagnosis |
| **AWS Agent** | AWS Infrastructure Specialist | EC2 health, GitHub Actions status |
| **Deploy Agent** | Deployment Specialist | rollout status, rollback, restart, history |
| **Cost Agent** | Cost Optimization Specialist | idle instances, unattached EBS, cost summary |
| **Incident Agent** | Incident Response Specialist | crash logs, cluster events, pod diagnosis |
| **Migration Agent** | CI/CD Migration Specialist | Jenkinsfile conversion, complexity analysis |

---

## How CrewAI Works Here

1. **User sends query** (or default health check runs)
2. **Manager agent** (auto-created by CrewAI hierarchical process) reads the tasks
3. Manager **delegates** each task to the most appropriate specialist agent
4. Each agent uses its **tools** (kubectl, boto3, GitHub API) to gather data
5. Agent returns findings to the manager
6. Manager produces a **final consolidated report**

---

## Configuration

### .env Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `GROQ_API_KEY` | Groq API key (free tier works) | `gsk_abc123...` |
| `CREWAI_LLM` | LLM model in LiteLLM format | `groq/llama-3.3-70b-versatile` |
| `GITHUB_TOKEN` | GitHub PAT for API access | `ghp_abc123...` |
| `TARGET_REPO` | Default repo to monitor | `ShivaKrishna44/devops-microservices-nojenkins` |
| `AWS_ACCESS_KEY_ID` | AWS credentials | `AKIA...` |
| `AWS_SECRET_ACCESS_KEY` | AWS credentials | `wJalr...` |
| `AWS_DEFAULT_REGION` | AWS region | `us-east-1` |

### Supported LLM Models (via Groq free tier)

| Model | Speed | Quality |
|-------|-------|---------|
| `groq/llama-3.3-70b-versatile` | Fast | Best (recommended) |
| `groq/mixtral-8x7b-32768` | Very fast | Good |
| `groq/gemma2-9b-it` | Fastest | Basic |

---

## Tool Matrix

| Category | Tool | Description |
|----------|------|-------------|
| **AWS** | `check_aws_ec2_health()` | Finds EC2 instances not running/healthy |
| **GitHub** | `check_github_workflow_status(repo)` | Checks last 10 runs for failures |
| **K8s** | `check_k8s_pod_health(namespace)` | Finds pods NOT Running/Completed |
| **K8s** | `check_k8s_node_health()` | Finds NotReady nodes |
| **K8s** | `check_k8s_deployments(namespace)` | Finds degraded deployments |
| **Deploy** | `check_rollout_status(deploy, ns)` | Rollout complete/stuck? |
| **Deploy** | `rollback_deployment(deploy, ns)` | Undo to previous version |
| **Deploy** | `restart_deployment(deploy, ns)` | Rolling restart |
| **Deploy** | `get_deployment_history(deploy, ns)` | Revision history |
| **Incident** | `get_pod_crash_logs(pod, ns)` | Previous container logs |
| **Incident** | `get_cluster_events(ns)` | Warning events |
| **Incident** | `diagnose_pod(pod, ns)` | Full pod diagnosis |
| **Cost** | `find_idle_ec2_instances()` | CPU < 5% over 24h |
| **Cost** | `find_unattached_ebs_volumes()` | Orphaned volumes |
| **Cost** | `get_cost_summary()` | Monthly spend by service |
| **Migration** | `convert_jenkinsfile_to_github_actions(content)` | Generate GHA YAML |
| **Migration** | `analyze_jenkinsfile_complexity(content)` | Tier 1/2/3 assessment |
